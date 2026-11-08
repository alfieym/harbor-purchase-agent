"""Aggregate a run and compare two runs behind a release gate.

Aggregation is macro (one case = one vote) and keeps expected-empty cases out
of the relevance averages. A comparison refuses to run when the two sides
measured different golden sets, catalogs, limits, or retrievers; only a
comparable pair can pass or fail the gate.
"""

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from harbor.eval.metrics import macro_mean, p95_nearest_rank
from harbor.eval.runner import CaseResult, RunReport

__all__ = [
    "Breakdown",
    "GateResult",
    "MetricDelta",
    "SummaryReport",
    "ViolationStat",
    "compare_runs",
    "summarize",
]

VIOLATION_CLASSES = ("budget", "material", "delivery")
_RANKING_METRICS = ("ndcg_at_5", "recall_at_5", "precision_at_5", "expected_empty_accuracy")
SMALL_SAMPLE = 20


class ViolationStat(BaseModel):
    """One constraint class: denominator, numerator, rate and coverage.

    ``rate is None`` when no case carried that constraint — "unknown", never
    "0% violated". ``coverage`` is the share of cases where the class applies.
    """

    applicable: int
    violations: int
    rate: float | None
    coverage: float
    violating_products: int


class Breakdown(BaseModel):
    """One slice of a run (overall, a bucket, or a split)."""

    key: str
    case_count: int
    positive_case_count: int
    expected_empty_case_count: int
    ndcg_at_5: float | None
    recall_at_5: float | None
    precision_at_5: float | None
    expected_empty_accuracy: float | None
    violations: dict[str, ViolationStat]
    latency_p95_ms: float | None
    latency_count: int
    latency_small_sample: bool
    degraded_case_count: int


class SummaryReport(BaseModel):
    overall: Breakdown
    by_bucket: dict[str, Breakdown]
    by_split: dict[str, Breakdown]


class MetricDelta(BaseModel):
    baseline: float | None
    candidate: float | None
    delta: float | None


class GateResult(BaseModel):
    comparable: bool
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    metrics: dict[str, MetricDelta] = Field(default_factory=dict)
    violations: dict[str, MetricDelta] = Field(default_factory=dict)
    latency_ratio: float | None = None


def summarize(report: RunReport) -> SummaryReport:
    """Aggregate one run overall, per bucket, and per split."""
    cases = list(report.case_results)
    by_bucket: dict[str, Breakdown] = {}
    by_split: dict[str, Breakdown] = {}
    for bucket in sorted({case.bucket for case in cases}):
        by_bucket[bucket] = _breakdown(bucket, [case for case in cases if case.bucket == bucket])
    for split in sorted({case.split for case in cases}):
        by_split[split] = _breakdown(split, [case for case in cases if case.split == split])
    return SummaryReport(
        overall=_breakdown("overall", cases),
        by_bucket=by_bucket,
        by_split=by_split,
    )


def compare_runs(
    baseline: RunReport,
    candidate: RunReport,
    *,
    min_metric_delta: float = -0.01,
    max_violation_delta: float = 0.01,
    max_latency_ratio: float = 1.2,
) -> GateResult:
    """Compare two runs with defaults that tolerate noise but not regressions."""
    reasons = _incompatibility_reasons(baseline, candidate)
    if reasons:
        return GateResult(comparable=False, passed=False, reasons=reasons)

    before = summarize(baseline).overall
    after = summarize(candidate).overall

    metric_deltas: dict[str, MetricDelta] = {}
    failures: list[str] = []
    for name in _RANKING_METRICS:
        baseline_value = getattr(before, name)
        candidate_value = getattr(after, name)
        delta = _delta(baseline_value, candidate_value)
        metric_deltas[name] = MetricDelta(
            baseline=baseline_value, candidate=candidate_value, delta=delta
        )
        if delta is not None and delta < min_metric_delta:
            failures.append(f"{name} dropped by {delta:.4f} (allowed {min_metric_delta:+.4f})")

    violation_deltas: dict[str, MetricDelta] = {}
    for name in VIOLATION_CLASSES:
        baseline_rate = before.violations[name].rate
        candidate_rate = after.violations[name].rate
        delta = _delta(baseline_rate, candidate_rate)
        violation_deltas[name] = MetricDelta(
            baseline=baseline_rate, candidate=candidate_rate, delta=delta
        )
        if delta is not None and delta > max_violation_delta:
            failures.append(
                f"violation rate {name} rose by {delta:.4f} (allowed {max_violation_delta:+.4f})"
            )

    latency_ratio = None
    if (
        before.latency_p95_ms is not None
        and after.latency_p95_ms is not None
        and before.latency_p95_ms > 0
    ):
        latency_ratio = after.latency_p95_ms / before.latency_p95_ms
        if latency_ratio > max_latency_ratio:
            failures.append(
                f"latency p95 rose {latency_ratio:.2f}x (allowed {max_latency_ratio:.2f}x)"
            )

    return GateResult(
        comparable=True,
        passed=not failures,
        failures=failures,
        metrics=metric_deltas,
        violations=violation_deltas,
        latency_ratio=latency_ratio,
    )


def _incompatibility_reasons(baseline: RunReport, candidate: RunReport) -> list[str]:
    reasons: list[str] = []
    if baseline.cases_sha256 != candidate.cases_sha256:
        reasons.append(f"golden cases differ ({baseline.cases_sha256} vs {candidate.cases_sha256})")
    if baseline.catalog_sha256 != candidate.catalog_sha256:
        reasons.append(f"catalog differs ({baseline.catalog_sha256} vs {candidate.catalog_sha256})")
    if baseline.limit != candidate.limit:
        reasons.append(f"limit differs ({baseline.limit} vs {candidate.limit})")
    if baseline.allow_degraded != candidate.allow_degraded:
        reasons.append("allow_degraded differs")
    if baseline.index_fingerprint != candidate.index_fingerprint:
        reasons.append(
            "index fingerprint differs "
            f"({baseline.index_fingerprint} vs {candidate.index_fingerprint})"
        )
    baseline_ids = [(case.case_id, case.split, case.bucket) for case in baseline.case_results]
    candidate_ids = [(case.case_id, case.split, case.bucket) for case in candidate.case_results]
    if baseline_ids != candidate_ids:
        reasons.append("case list differs")
    if baseline.degraded_case_count:
        reasons.append(f"baseline has {baseline.degraded_case_count} degraded case(s)")
    if candidate.degraded_case_count:
        reasons.append(f"candidate has {candidate.degraded_case_count} degraded case(s)")
    return reasons


def _breakdown(key: str, cases: Sequence[CaseResult]) -> Breakdown:
    positives = [case for case in cases if not case.expected_empty]
    empties = [case for case in cases if case.expected_empty]
    latencies = [case.latency_ms for case in cases]

    violations = {name: _violation_stat(name, cases) for name in VIOLATION_CLASSES}
    return Breakdown(
        key=key,
        case_count=len(cases),
        positive_case_count=len(positives),
        expected_empty_case_count=len(empties),
        ndcg_at_5=_mean(case.ndcg_at_5 for case in positives),
        recall_at_5=_mean(case.recall_at_5 for case in positives),
        precision_at_5=_mean(case.precision_at_5 for case in positives),
        expected_empty_accuracy=_mean(
            1.0 if case.expected_empty_correct else 0.0 for case in empties
        ),
        violations=violations,
        latency_p95_ms=p95_nearest_rank(latencies),
        latency_count=len(latencies),
        latency_small_sample=len(latencies) < SMALL_SAMPLE,
        degraded_case_count=sum(1 for case in cases if case.recall_strategy != "embedding"),
    )


def _violation_stat(name: str, cases: Sequence[CaseResult]) -> ViolationStat:
    applicable = [case for case in cases if case.applicable.get(name, False)]
    violations = [case for case in applicable if case.violations.get(name, False)]
    offending = sum(len(case.violating_products.get(name, [])) for case in cases)
    return ViolationStat(
        applicable=len(applicable),
        violations=len(violations),
        rate=(len(violations) / len(applicable)) if applicable else None,
        coverage=(len(applicable) / len(cases)) if cases else 0.0,
        violating_products=offending,
    )


def _mean(values: Iterable[float | None]) -> float | None:
    return macro_mean(values)


def _delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return candidate - baseline
