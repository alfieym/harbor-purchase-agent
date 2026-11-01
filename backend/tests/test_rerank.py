"""S3: reranker protocol, reorder semantics, and rerank-status reporting."""

from collections.abc import Sequence
from datetime import date

from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.rerank import apply_rerank


class FakeReranker:
    """Returns the scores it was given, one per document, in document order."""

    def __init__(self, scores: Sequence[float]) -> None:
        self._scores = list(scores)

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        assert len(documents) == len(self._scores)
        return list(self._scores)


class ExplodingReranker:
    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        raise RuntimeError("boom")


def _product(product_id: str, title: str) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=title,
        brand="TestBrand",
        category="母婴宠物",
        tax_category="母婴宠物",
        description="",
        highlights=[],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=0.5,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-S1",
                spec="标准",
                price=Money.from_major_units(100, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_apply_rerank_reorders_top_k_and_reports_success() -> None:
    scored = [(_product("A", "a"), 0.9), (_product("B", "b"), 0.8), (_product("C", "c"), 0.7)]
    reranker = FakeReranker([0.1, 0.9, 0.5])  # B > C > A
    out, status = apply_rerank(reranker, "q", scored, top_k=3)
    assert [p.product_id for p, _ in out] == ["B", "C", "A"]
    assert status == "success"


def test_apply_rerank_keeps_tail_order() -> None:
    scored = [(_product("A", "a"), 0.9), (_product("B", "b"), 0.8), (_product("C", "c"), 0.7)]
    reranker = FakeReranker([0.0, 1.0])  # only top 2 reranked
    out, status = apply_rerank(reranker, "q", scored, top_k=2)
    assert [p.product_id for p, _ in out] == ["B", "A", "C"]
    assert status == "success"


def test_apply_rerank_disabled_reports_disabled() -> None:
    scored = [(_product("A", "a"), 0.9)]
    out, status = apply_rerank(None, "q", scored, top_k=5)
    assert out == scored
    assert status == "disabled"


def test_rerank_failure_degrades_and_reports_failed() -> None:
    scored = [(_product("A", "a"), 0.9), (_product("B", "b"), 0.8)]
    out, status = apply_rerank(ExplodingReranker(), "q", scored, top_k=2)
    assert [p.product_id for p, _ in out] == ["A", "B"]
    assert status == "failed"


def test_zero_top_k_reports_disabled() -> None:
    scored = [(_product("A", "a"), 0.9)]
    out, status = apply_rerank(FakeReranker([1.0]), "q", scored, top_k=0)
    assert out == scored
    assert status == "disabled"
