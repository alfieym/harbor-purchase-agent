"""Report aggregation and the paired comparison gate.

The gate's first job is refusing to compare runs that measured different
things; its second is refusing a regression. Both are tested here.
"""

import pytest

from harbor.eval.report import compare_runs, summarize
from harbor.eval.runner import CaseResult, RunReport


def case_result(
    case_id="c1",
    *,
    expected_empty=False,
    ndcg=None,
    recall=None,
    precision=None,
    empty_correct=None,
    latency=10.0,
    bucket="bucket",
    split="test",
    applicable=None,
    violations=None,
    offending=None,
    strategy="embedding",
    hits=None,
    relevant_count=1,
):
    hits = list(hits or [])
    applicable = applicable or {"budget": False, "material": False, "delivery": False}
    violations = violations or {"budget": False, "material": False, "delivery": False}
    offending = offending or {"budget": [], "material": [], "delivery": []}
    return CaseResult(
        case_id=case_id,
        seed_id=case_id,
        bucket=bucket,
        split=split,
        query=case_id,
        expected_empty=expected_empty,
        hits=hits,
        hit_count=len(hits),
        relevant_count=relevant_count,
        ndcg_at_5=ndcg,
        recall_at_5=recall,
        precision_at_5=precision,
        expected_empty_correct=empty_correct,
        latency_ms=latency,
        recall_strategy=strategy,
        rerank_status="disabled",
        applicable=applicable,
        violations=violations,
        violating_products=offending,
    )


def report(cases, *, golden="g1", catalog="cat1", limit=10, degraded=False):
    return RunReport(
        golden_version="v1",
        cases_sha256=golden,
        catalog_sha256=catalog,
        limit=limit,
        allow_degraded=degraded,
        case_results=list(cases),
    )


def positive(case_id="p1", value=1.0, **kwargs):
    return case_result(
        case_id,
        ndcg=value,
        recall=value,
        precision=value,
        **kwargs,
    )


def empty(case_id="e1", correct=True, **kwargs):
    return case_result(case_id, expected_empty=True, empty_correct=correct, **kwargs)


def test_positive_metrics_are_macro_averaged_and_include_zero_hit_cases():
    summary = summarize(report([positive("p1", 1.0), positive("p2", 0.0), empty("e1", True)]))
    assert summary.overall.ndcg_at_5 == pytest.approx(0.5)
    assert summary.overall.recall_at_5 == pytest.approx(0.5)
    assert summary.overall.precision_at_5 == pytest.approx(0.5)
    assert summary.overall.positive_case_count == 2


def test_expected_empty_accuracy_is_over_empty_cases_only():
    summary = summarize(report([positive("p1", 1.0), empty("e1", True), empty("e2", False)]))
    assert summary.overall.expected_empty_accuracy == pytest.approx(0.5)
    assert summary.overall.expected_empty_case_count == 2


def test_violation_rate_is_pair_weighted_and_reports_coverage():
    violated = {"budget": True, "material": False, "delivery": False}
    clean = {"budget": False, "material": False, "delivery": False}
    budget_applies = {"budget": True, "material": False, "delivery": False}
    summary = summarize(
        report(
            [
                positive(
                    "p1",
                    1.0,
                    applicable=budget_applies,
                    violations=violated,
                    offending={"budget": ["prod-1"], "material": [], "delivery": []},
                ),
                positive("p2", 1.0, applicable=budget_applies, violations=clean),
                positive("p3", 1.0),
            ]
        )
    )
    budget = summary.overall.violations["budget"]
    assert (budget.applicable, budget.violations) == (2, 1)
    assert budget.rate == pytest.approx(0.5)
    assert budget.coverage == pytest.approx(2 / 3)
    assert budget.violating_products == 1

    material = summary.overall.violations["material"]
    assert material.applicable == 0
    assert material.rate is None
    assert material.coverage == 0.0


def test_violating_product_count_sums_every_displayed_offender():
    applies = {"budget": True, "material": False, "delivery": False}
    violated = {"budget": True, "material": False, "delivery": False}
    summary = summarize(
        report(
            [
                positive(
                    "p1",
                    1.0,
                    applicable=applies,
                    violations=violated,
                    offending={"budget": ["a", "b"], "material": [], "delivery": []},
                ),
                positive(
                    "p2",
                    1.0,
                    applicable=applies,
                    violations=violated,
                    offending={"budget": ["c"], "material": [], "delivery": []},
                ),
            ]
        )
    )
    assert summary.overall.violations["budget"].violating_products == 3


def test_p95_is_nearest_rank_and_flags_a_small_sample():
    certain = summarize(
        report(
            [
                positive(f"p{i}", 1.0, latency=latency)
                for i, latency in enumerate([10.0, 20.0, 30.0])
            ]
        )
    )
    assert certain.overall.latency_p95_ms == pytest.approx(30.0)
    assert certain.overall.latency_small_sample is True

    many = summarize(report([positive(f"p{i}", 1.0, latency=float(i + 1)) for i in range(20)]))
    assert many.overall.latency_p95_ms == pytest.approx(19.0)
    assert many.overall.latency_small_sample is False


def test_breakdowns_exist_per_bucket_and_per_split():
    cases = [
        positive("p1", 1.0, bucket="a", split="dev"),
        positive("p2", 0.0, bucket="a", split="test"),
        empty("e1", True, bucket="b", split="dev"),
    ]
    summary = summarize(report(cases))
    assert set(summary.by_bucket) == {"a", "b"}
    assert summary.by_bucket["a"].case_count == 2
    assert summary.by_bucket["a"].ndcg_at_5 == pytest.approx(0.5)
    assert set(summary.by_split) == {"dev", "test"}
    assert summary.by_split["dev"].case_count == 2


def test_compare_requires_the_same_golden_hash():
    result = compare_runs(
        report([positive("p1")], golden="g1"),
        report([positive("p1")], golden="g2"),
    )
    assert result.comparable is False
    assert result.passed is False
    assert any("golden" in reason for reason in result.reasons)


def test_compare_requires_the_same_catalog_and_limit():
    result = compare_runs(
        report([positive("p1")], catalog="cat1", limit=10),
        report([positive("p1")], catalog="cat2", limit=10),
    )
    assert any("catalog" in reason for reason in result.reasons)

    result = compare_runs(
        report([positive("p1")], limit=10),
        report([positive("p1")], limit=20),
    )
    assert any("limit" in reason for reason in result.reasons)


def test_compare_rejects_a_run_with_a_degraded_case():
    degraded = positive("p1", strategy="keyword_2gram")
    result = compare_runs(report([positive("p1")]), report([degraded]))
    assert result.comparable is False
    assert any("degrad" in reason for reason in result.reasons)


def test_compare_passes_on_equal_runs():
    baseline = report([positive("p1", 0.8), empty("e1", True)])
    candidate = report([positive("p1", 0.8), empty("e1", True)])
    result = compare_runs(baseline, candidate)
    assert result.comparable is True
    assert result.passed is True
    assert result.metrics["ndcg_at_5"].delta == pytest.approx(0.0)


def test_compare_fails_when_ndcg_drops_below_the_threshold():
    baseline = report([positive("p1", 0.9)])
    candidate = report([positive("p1", 0.8)])
    result = compare_runs(baseline, candidate)
    assert result.passed is False
    assert any("ndcg_at_5" in failure for failure in result.failures)


def test_compare_tolerates_a_drop_within_the_threshold():
    baseline = report([positive("p1", 0.900)])
    candidate = report([positive("p1", 0.895)])
    result = compare_runs(baseline, candidate)
    assert result.passed is True


def test_compare_fails_when_a_violation_rate_rises():
    applies = {"budget": True, "material": False, "delivery": False}
    clean = {"budget": False, "material": False, "delivery": False}
    dirty = {"budget": True, "material": False, "delivery": False}
    baseline = report([positive("p1", applicable=applies, violations=clean)])
    candidate = report([positive("p1", applicable=applies, violations=dirty)])
    result = compare_runs(baseline, candidate)
    assert result.passed is False
    assert any("budget" in failure for failure in result.failures)


def test_compare_fails_when_p95_ratio_exceeds_the_threshold():
    baseline = report([positive("p1", latency=100.0)])
    candidate = report([positive("p1", latency=130.0)])
    result = compare_runs(baseline, candidate)
    assert result.latency_ratio == pytest.approx(1.3)
    assert result.passed is False
    assert any("latency" in failure.lower() for failure in result.failures)


def test_compare_skips_a_metric_with_no_data_instead_of_failing():
    # No expected-empty cases on either side: accuracy is None, not a failure.
    baseline = report([positive("p1", 1.0)])
    candidate = report([positive("p1", 1.0)])
    result = compare_runs(baseline, candidate)
    assert result.metrics["expected_empty_accuracy"].delta is None
    assert result.passed is True
