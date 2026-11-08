"""Run a frozen golden set against one retrieval service.

The runner is deliberately thin: one search per case, at the same ``limit`` the
product tool uses, recording exactly what the retriever returned. No retries, no
re-ranking decisions, no filtering — any "fixing" here would measure the runner
instead of the system.
"""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from harbor.eval.metrics import ndcg_at_k, precision_at_k, recall_at_k
from harbor.eval.schema import GoldenCase, GoldenSet
from harbor.features.catalog.search.constraints import facts_from_product, reasons_for
from harbor.features.catalog.search.schemas import SearchResult

__all__ = ["CaseResult", "EvalRunError", "RunReport", "run_golden"]

K = 5
_VIOLATION_CLASSES = ("budget", "material", "delivery")


class EvalRunError(RuntimeError):
    """The run cannot be compared: it did not use the semantic strategy."""


class CaseResult(BaseModel):
    """One case's retrieved canonicals and its scores."""

    case_id: str
    seed_id: str
    bucket: str
    split: str
    query: str
    expected_empty: bool
    hits: list[str]
    hit_count: int
    relevant_count: int
    ndcg_at_5: float | None = None
    recall_at_5: float | None = None
    precision_at_5: float | None = None
    expected_empty_correct: bool | None = None
    latency_ms: float
    recall_strategy: str
    rerank_status: str
    applicable: dict[str, bool] = Field(default_factory=dict)
    violations: dict[str, bool] = Field(default_factory=dict)
    violating_products: dict[str, list[str]] = Field(default_factory=dict)


class RunReport(BaseModel):
    """Everything a report or a paired comparison needs from one run."""

    golden_version: str
    cases_sha256: str
    catalog_sha256: str
    limit: int
    allow_degraded: bool
    case_results: list[CaseResult]
    warm_up_latency_ms: float | None = None
    # The catalog fingerprint the index was built from. A paired comparison
    # refuses to run when the two sides searched different indexes.
    index_fingerprint: str | None = None

    @property
    def degraded_case_count(self) -> int:
        return sum(1 for case in self.case_results if case.recall_strategy != "embedding")

    @property
    def strategies(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.case_results:
            counts[case.recall_strategy] = counts.get(case.recall_strategy, 0) + 1
        return counts


def run_golden(
    golden: GoldenSet,
    service: Any,
    *,
    limit: int = 10,
    allow_degraded: bool = False,
    warm_up: bool = True,
    index_fingerprint: str | None = None,
) -> RunReport:
    """Search every case once and return per-case measurements.

    ``service`` is anything with ``search(query, spec, limit) -> SearchResult``.
    A warm-up search is made once so the first case does not pay the model's
    cold-start cost; its latency is kept separately and never scored.
    """
    cases = list(golden.cases)
    warm_up_latency = None
    if warm_up and cases:
        first = cases[0]
        warm = service.search(first.query, first.spec, limit)
        warm_up_latency = _latency(warm)

    results = [
        _run_case(case, service.search(case.query, case.spec, limit), limit, allow_degraded)
        for case in cases
    ]
    return RunReport(
        golden_version=golden.version,
        cases_sha256=golden.manifest.cases_sha256,
        catalog_sha256=golden.catalog_sha256,
        limit=limit,
        allow_degraded=allow_degraded,
        case_results=results,
        warm_up_latency_ms=warm_up_latency,
        index_fingerprint=index_fingerprint,
    )


def _run_case(
    case: GoldenCase,
    result: SearchResult,
    limit: int,
    allow_degraded: bool,
) -> CaseResult:
    if result.recall_strategy != "embedding" and not allow_degraded:
        raise EvalRunError(
            f"case {case.case_id} used recall strategy "
            f"{result.recall_strategy!r}; only 'embedding' is a valid evaluation "
            "run (pass allow_degraded=True to record it instead)"
        )

    hits = list(result.hits[:limit])
    canonicals = [hit.product.canonical_product_id for hit in hits]
    relevant = set(case.relevant_canonicals)

    if case.expected_empty:
        ndcg = recall = precision = None
        empty_correct = not hits
    else:
        ndcg = ndcg_at_k(relevant, canonicals, K)
        recall = recall_at_k(relevant, canonicals, K)
        precision = precision_at_k(relevant, canonicals, K)
        empty_correct = None

    applicable, violations, violating_products = _check_violations(case, hits)

    return CaseResult(
        case_id=case.case_id,
        seed_id=case.seed_id,
        bucket=case.bucket,
        split=case.split,
        query=case.query,
        expected_empty=case.expected_empty,
        hits=canonicals,
        hit_count=len(hits),
        relevant_count=len(relevant),
        ndcg_at_5=ndcg,
        recall_at_5=recall,
        precision_at_5=precision,
        expected_empty_correct=empty_correct,
        latency_ms=_latency(result),
        recall_strategy=result.recall_strategy,
        rerank_status=result.diagnostics.rerank_status,
        applicable=applicable,
        violations=violations,
        violating_products=violating_products,
    )


def _check_violations(
    case: GoldenCase, hits: Sequence[Any]
) -> tuple[dict[str, bool], dict[str, bool], dict[str, list[str]]]:
    """Evaluate each displayed product against the case's hard constraints.

    Budget and material reuse ``reasons_for`` — the same oracle the retriever's
    Qdrant filter is written against. Delivery is a diagnostic: search has no
    destination input, so a non-shipping hit is recorded but does not by itself
    invalidate a case.
    """
    spec = case.spec
    applicable = {
        "budget": spec.price_max_major is not None,
        "material": bool(spec.excluded_material_tags),
        "delivery": case.destination_country is not None,
    }
    violations = {name: False for name in _VIOLATION_CLASSES}
    offending: dict[str, list[str]] = {name: [] for name in _VIOLATION_CLASSES}

    for hit in hits:
        product = hit.product
        codes = {reason.code for reason in reasons_for(facts_from_product(product), spec)}
        if "price_over" in codes and applicable["budget"]:
            violations["budget"] = True
            offending["budget"].append(product.product_id)
        if "excluded_material" in codes and applicable["material"]:
            violations["material"] = True
            offending["material"].append(product.product_id)
        if applicable["delivery"] and case.destination_country not in product.ships_to:
            violations["delivery"] = True
            offending["delivery"].append(product.product_id)

    return applicable, violations, offending


def _latency(result: SearchResult) -> float:
    return float(result.diagnostics.latency_ms)
