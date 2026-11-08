"""S12 runner: one retrieval per golden case, measured exactly as returned.

The fake service makes each failure mode explicit — duplicate hits, a
fallback strategy, a violation — without touching Qdrant or an embedder.
"""

from datetime import date
from decimal import Decimal

import pytest

from harbor.eval.runner import EvalRunError, run_golden
from harbor.eval.schema import GoldenCase, GoldenManifest, GoldenSet
from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.schemas import (
    SearchDiagnostics,
    SearchHit,
    SearchResult,
)


def product(
    product_id,
    canonical,
    *,
    price="100",
    materials=("金属",),
    ships=("CN",),
    category="户外运动",
):
    return Product(
        product_id=product_id,
        canonical_product_id=canonical,
        external_product_id=product_id,
        title=f"Product {product_id}",
        brand="Acme",
        category=category,
        tax_category="general",
        description="",
        highlights=[],
        material_tags=list(materials),
        origin_country="CN",
        ships_to=list(ships),
        source_platform="shop",
        dimensions_cm={"length": 1, "width": 1, "height": 1},
        weight_kg=0.5,
        rating_summary={"score": 4.5, "count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-s",
                spec="std",
                price=Money.from_major_units(price, "CNY"),
                stock=5,
            )
        ],
        updated_at=date(2026, 1, 1),
    )


def hit(item, score=1.0):
    return SearchHit(product=item, score=score, selected_sku=item.skus[0])


def result(hits, *, latency=1.0, strategy="embedding", rerank="disabled"):
    return SearchResult(
        hits=hits,
        total=len(hits),
        recall_strategy=strategy,
        diagnostics=SearchDiagnostics(latency_ms=latency, rerank_status=rerank),
    )


def case(
    case_id="c1",
    *,
    query=None,
    relevant=("C1",),
    expected_empty=False,
    spec=None,
    destination=None,
):
    query = query or case_id
    return GoldenCase(
        case_id=case_id,
        seed_id=case_id,
        bucket="bucket",
        split="test",
        query=query,
        spec=spec or ProductSearchSpec(normalized_query=query, category="户外运动"),
        destination_country=destination,
        expected_empty=expected_empty,
        relevant_canonicals=list(relevant),
    )


def golden(cases):
    manifest = GoldenManifest(
        version="t",
        generator_version=1,
        catalog_sha256="cat",
        cases_sha256="cases",
        seed_count=len(cases),
        case_count=len(cases),
        split_counts={"test": len(cases)},
        bucket_counts={"bucket": len(cases)},
        bucket_split_counts={"bucket": {"test": len(cases)}},
    )
    return GoldenSet(
        version="t",
        generator_version=1,
        catalog_sha256="cat",
        cases=list(cases),
        manifest=manifest,
    )


class FakeService:
    def __init__(self, results, *, warm_up=None):
        self.results = results
        self.warm_up = warm_up
        self.calls = []

    def search(self, query, spec, limit=10):
        self.calls.append((query, spec, limit))
        if len(self.calls) == 1 and self.warm_up is not None:
            return self.warm_up
        return self.results[query]


def test_runner_records_canonical_ids_in_rank_order_including_duplicates():
    p1, p2 = product("p1", "C1"), product("p2", "C1")
    service = FakeService({"q": result([hit(p1), hit(p2)])})
    report = run_golden(golden([case("q")]), service)
    assert report.case_results[0].hits == ["C1", "C1"]


def test_runner_computes_the_three_ranking_metrics_for_a_positive_case():
    c1, c2, c3 = (
        product("p1", "C1"),
        product("p2", "C2"),
        product("p3", "C3"),
    )
    service = FakeService({"q": result([hit(c1), hit(c3), hit(c2)])})
    report = run_golden(golden([case("q", relevant=("C1", "C2"))]), service)
    case_result = report.case_results[0]
    assert case_result.ndcg_at_5 == pytest.approx(1.5 / (1.0 + 1.0 / 1.584962500721156))
    assert case_result.recall_at_5 == pytest.approx(1.0)
    assert case_result.precision_at_5 == pytest.approx(2 / 3)
    assert case_result.expected_empty_correct is None


def test_runner_keeps_expected_empty_cases_out_of_relevance_metrics():
    c1 = product("p1", "C1")
    service = FakeService({"pos": result([hit(c1)]), "empty": result([hit(c1)])})
    report = run_golden(
        golden([case("pos", relevant=("C1",)), case("empty", expected_empty=True, relevant=())]),
        service,
    )
    positive, empty = report.case_results
    assert positive.ndcg_at_5 == pytest.approx(1.0)
    assert empty.ndcg_at_5 is None
    assert empty.recall_at_5 is None
    assert empty.precision_at_5 is None
    assert empty.expected_empty_correct is False


def test_runner_scores_a_positive_case_with_no_hits_as_zero():
    service = FakeService({"q": result([])})
    report = run_golden(golden([case("q")]), service)
    case_result = report.case_results[0]
    assert (case_result.ndcg_at_5, case_result.recall_at_5, case_result.precision_at_5) == (
        0.0,
        0.0,
        0.0,
    )


def test_runner_flags_a_budget_violation():
    expensive = product("p1", "C1", price="200")
    spec = ProductSearchSpec(normalized_query="q", price_max_major=Decimal("100"))
    service = FakeService({"q": result([hit(expensive)])})
    report = run_golden(golden([case("q", spec=spec)]), service)
    case_result = report.case_results[0]
    assert case_result.applicable["budget"] is True
    assert case_result.violations["budget"] is True
    assert case_result.violating_products["budget"] == ["p1"]


def test_runner_flags_a_material_violation():
    plastic = product("p1", "C1", materials=("合成聚合物",))
    spec = ProductSearchSpec(normalized_query="q", excluded_material_tags=["合成聚合物"])
    service = FakeService({"q": result([hit(plastic)])})
    report = run_golden(golden([case("q", spec=spec)]), service)
    assert report.case_results[0].violations["material"] is True


def test_runner_flags_delivery_only_when_a_destination_is_set():
    domestic = product("p1", "C1", ships=("CN",))
    service = FakeService({"q": result([hit(domestic)])})
    without = run_golden(golden([case("q")]), service).case_results[0]
    with_destination = run_golden(
        golden([case("q", destination="US")]), FakeService({"q": result([hit(domestic)])})
    ).case_results[0]
    assert without.applicable["delivery"] is False
    assert without.violations["delivery"] is False
    assert with_destination.applicable["delivery"] is True
    assert with_destination.violations["delivery"] is True
    assert with_destination.violating_products["delivery"] == ["p1"]


def test_runner_fails_closed_on_a_non_embedding_strategy():
    service = FakeService({"q": result([], strategy="keyword_2gram")})
    with pytest.raises(EvalRunError) as excinfo:
        run_golden(golden([case("q")]), service)
    assert "keyword_2gram" in str(excinfo.value)


def test_allow_degraded_records_the_fallback_instead_of_raising():
    service = FakeService({"q": result([], strategy="keyword_2gram")})
    report = run_golden(golden([case("q")]), service, allow_degraded=True)
    case_result = report.case_results[0]
    assert case_result.recall_strategy == "keyword_2gram"
    assert report.degraded_case_count == 1


def test_runner_uses_search_latency_and_caps_hits_at_limit():
    c1, c2 = product("p1", "C1"), product("p2", "C2")
    service = FakeService({"q": result([hit(c1), hit(c2)], latency=12.5)})
    report = run_golden(golden([case("q", relevant=("C1",))]), service, limit=1)
    case_result = report.case_results[0]
    assert case_result.latency_ms == pytest.approx(12.5)
    assert case_result.hits == ["C1"]
    assert service.calls[0][2] == 1


def test_warm_up_runs_once_and_is_not_scored():
    c1 = product("p1", "C1")
    service = FakeService(
        {"q1": result([hit(c1)]), "q2": result([hit(c1)])},
        warm_up=result([], latency=999.0),
    )
    report = run_golden(golden([case("q1"), case("q2")]), service, limit=10)
    assert len(service.calls) == 3
    assert service.calls[0][0] == "q1"
    assert [case.latency_ms for case in report.case_results] == [1.0, 1.0]
    assert report.warm_up_latency_ms == pytest.approx(999.0)
