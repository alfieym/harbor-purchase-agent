"""CLI command handlers and a thin argparse front end.

Command handlers are plain functions so tests can pass a fake search service;
only ``main`` touches the real Qdrant index and embedder.
"""

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from harbor.eval.golden import (
    build_golden_set,
    load_golden,
    save_golden,
    verify_golden,
)
from harbor.eval.report import SummaryReport, compare_runs, summarize
from harbor.eval.runner import RunReport, run_golden
from harbor.eval.schema import EvalSeed, GoldenSet
from harbor.features.catalog.models import Product

__all__ = [
    "build_command",
    "compare_command",
    "format_gate",
    "format_summary",
    "main",
    "report_command",
    "run_command",
    "verify_command",
]

DEFAULT_GOLDEN = "data/eval/s12-golden-v1.json"


def build_command(
    products: Sequence[Product],
    seeds: Sequence[EvalSeed],
    *,
    out: str | Path,
    catalog_sha256: str,
    version: str = "s12-golden-v1",
    bucket_quotas: Mapping[str, int] | None = None,
) -> GoldenSet:
    """Derive, self-check, freeze and write the golden artifact."""
    golden = build_golden_set(
        products,
        seeds,
        version=version,
        catalog_sha256=catalog_sha256,
        bucket_quotas=bucket_quotas,
    )
    save_golden(golden, out)
    return golden


def verify_command(golden_path: str | Path) -> GoldenSet:
    """Load and verify a frozen golden file; raises ``GoldenSetError`` on drift."""
    golden = load_golden(golden_path)
    verify_golden(golden)
    return golden


def run_command(
    golden_path: str | Path,
    service: Any,
    *,
    out: str | Path,
    limit: int = 10,
    allow_degraded: bool = False,
    index_fingerprint: str | None = None,
) -> RunReport:
    """Run one frozen golden set and write the per-case report."""
    golden = verify_command(golden_path)
    report = run_golden(
        golden,
        service,
        limit=limit,
        allow_degraded=allow_degraded,
        index_fingerprint=index_fingerprint,
    )
    _write_json(report.model_dump(mode="json"), out)
    return report


def report_command(run_path: str | Path) -> SummaryReport:
    return summarize(load_report(run_path))


def compare_command(
    baseline_path: str | Path,
    candidate_path: str | Path,
    **thresholds: float,
) -> Any:
    return compare_runs(load_report(baseline_path), load_report(candidate_path), **thresholds)


def load_report(path: str | Path) -> RunReport:
    return RunReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def format_summary(summary: SummaryReport) -> str:
    overall = summary.overall
    lines = [
        f"cases={overall.case_count} "
        f"positive={overall.positive_case_count} "
        f"expected_empty={overall.expected_empty_case_count}",
        f"NDCG@5={_num(overall.ndcg_at_5)} "
        f"Recall@5={_num(overall.recall_at_5)} "
        f"Precision@5={_num(overall.precision_at_5)} "
        f"empty_acc={_num(overall.expected_empty_accuracy)}",
        f"latency p95={_num(overall.latency_p95_ms)} ms "
        f"(n={overall.latency_count}"
        f"{', small sample' if overall.latency_small_sample else ''})",
    ]
    for name in ("budget", "material", "delivery"):
        stat = overall.violations[name]
        lines.append(
            f"violation {name}: {stat.violations}/{stat.applicable} "
            f"rate={_num(stat.rate)} coverage={stat.coverage:.1%} "
            f"products={stat.violating_products}"
        )
    for bucket, breakdown in summary.by_bucket.items():
        lines.append(
            f"  [{bucket}] n={breakdown.case_count} "
            f"NDCG@5={_num(breakdown.ndcg_at_5)} "
            f"empty_acc={_num(breakdown.expected_empty_accuracy)} "
            f"p95={_num(breakdown.latency_p95_ms)}ms"
        )
    return "\n".join(lines)


def format_gate(gate: Any) -> str:
    lines = [
        f"comparable={gate.comparable} passed={gate.passed}",
    ]
    if gate.reasons:
        lines.append("reasons: " + "; ".join(gate.reasons))
    if gate.failures:
        lines.append("failures: " + "; ".join(gate.failures))
    for name, delta in gate.metrics.items():
        lines.append(
            f"  {name}: {_num(delta.baseline)} -> {_num(delta.candidate)} "
            f"(delta {_num(delta.delta)})"
        )
    for name, delta in gate.violations.items():
        lines.append(
            f"  violation {name}: {_num(delta.baseline)} -> {_num(delta.candidate)} "
            f"(delta {_num(delta.delta)})"
        )
    if gate.latency_ratio is not None:
        lines.append(f"  latency ratio: {gate.latency_ratio:.2f}x")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harbor.eval")
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser("build", help="derive and freeze the golden set")
    build_parser.add_argument("--out", default=DEFAULT_GOLDEN)
    build_parser.add_argument("--version", default="s12-golden-v1")

    verify_parser = sub.add_parser("verify", help="verify a frozen golden set")
    verify_parser.add_argument("--golden", default=DEFAULT_GOLDEN)

    run_parser = sub.add_parser("run", help="run the golden set against the real index")
    run_parser.add_argument("--golden", default=DEFAULT_GOLDEN)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--limit", type=int, default=10)
    run_parser.add_argument("--allow-degraded", action="store_true")

    report_parser = sub.add_parser("report", help="summarize a run report")
    report_parser.add_argument("--run", required=True)

    compare_parser = sub.add_parser("compare", help="paired gate for two run reports")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--min-metric-delta", type=float, default=-0.01)
    compare_parser.add_argument("--max-violation-delta", type=float, default=0.01)
    compare_parser.add_argument("--max-latency-ratio", type=float, default=1.2)

    args = parser.parse_args(argv)

    if args.command == "build":
        from harbor.agents.factory import DEFAULT_DATA_PATH
        from harbor.eval.seeds import BUCKET_QUOTAS, SEEDS
        from harbor.features.catalog.persistence.seed_products import load_products

        data = Path(DEFAULT_DATA_PATH)
        products = load_products(data)
        golden = build_command(
            products,
            SEEDS,
            out=args.out,
            catalog_sha256=hashlib.sha256(data.read_bytes()).hexdigest(),
            version=args.version,
            bucket_quotas=BUCKET_QUOTAS,
        )
        print(
            f"wrote {args.out}: {golden.manifest.case_count} cases, "
            f"splits {golden.manifest.split_counts}, "
            f"hash {golden.manifest.cases_sha256}"
        )
        return 0

    if args.command == "verify":
        golden = verify_command(args.golden)
        print(
            f"ok {args.golden}: {golden.manifest.case_count} cases, "
            f"hash {golden.manifest.cases_sha256}"
        )
        return 0

    if args.command == "run":
        service, fingerprint = _open_real_service()
        report = run_command(
            args.golden,
            service,
            out=args.out,
            limit=args.limit,
            allow_degraded=args.allow_degraded,
            index_fingerprint=fingerprint,
        )
        print(format_summary(summarize(report)))
        print(f"wrote {args.out}")
        return 0

    if args.command == "report":
        print(format_summary(report_command(args.run)))
        return 0

    if args.command == "compare":
        gate = compare_command(
            args.baseline,
            args.candidate,
            min_metric_delta=args.min_metric_delta,
            max_violation_delta=args.max_violation_delta,
            max_latency_ratio=args.max_latency_ratio,
        )
        print(format_gate(gate))
        return 0 if gate.passed else 1

    return 2


def _open_real_service() -> tuple[Any, str]:
    """Open the same index and reranker the product tool uses, read-only."""
    from harbor.agents.factory import (
        DEFAULT_DATA_PATH,
        FINGERPRINT_PATH,
        _embedders,
        _open_index,
    )
    from harbor.core.config import Settings
    from harbor.features.catalog.persistence.seed_products import load_products
    from harbor.features.catalog.search.rerank import FastembedReranker
    from harbor.features.catalog.search.service import CatalogSearchService

    settings = Settings()
    data = Path(DEFAULT_DATA_PATH)
    products = load_products(data)
    embedder, index_embedder = _embedders(settings)
    client, fingerprint = _open_index(data, settings, degraded=index_embedder is not None)
    reranker = (
        FastembedReranker(settings.search_rerank_model) if settings.search_rerank_enabled else None
    )
    service = CatalogSearchService(
        products,
        embedder,
        reranker=reranker,
        index_embedder=index_embedder,
        client=client,
    )
    FINGERPRINT_PATH.write_text(fingerprint, encoding="utf-8")
    return service, fingerprint


def _write_json(payload: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
