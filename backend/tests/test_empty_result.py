"""S2-5: empty results carry an explanation instead of silently returning [].

A nonexistent category says so; an existing category with no matching products
gives a generic "no match" reason. Non-empty results have no reason.
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch


class FakeEmbedder:
    """Deterministic character-frequency embedder; no network, no model."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            counts = [0.0] * self._dim
            for char in text:
                counts[ord(char) % self._dim] += 1.0
            norm = math.sqrt(sum(value * value for value in counts)) or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


def _product(product_id: str, category: str, price_major: float) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=f"{product_id} {category} 商品",
        brand="TestBrand",
        category=category,
        tax_category=category,
        description=f"{category} 描述",
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
                price=Money.from_major_units(price_major, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_missing_category_reports_it_in_pipeline() -> None:
    products = [_product("P1", "母婴宠物", 500)]
    spec = ProductSearchSpec(category="不存在的品类", currency="CNY")

    result = SearchPipeline(FakeEmbedder()).search("宠物", spec, products, limit=10)

    assert result.hits == []
    assert result.empty_reason == "品类「不存在的品类」不存在"


def test_missing_category_reports_it_in_qdrant() -> None:
    products = [_product("P1", "母婴宠物", 500)]
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(products)

    result = QdrantProductSearch(client, FakeEmbedder()).search(
        "宠物", ProductSearchSpec(category="不存在的品类", currency="CNY"), limit=10
    )

    assert result.hits == []
    assert result.empty_reason == "品类「不存在的品类」不存在"


def test_existing_category_no_match_gives_generic_reason() -> None:
    products = [_product("P1", "母婴宠物", 500)]
    spec = ProductSearchSpec(category="母婴宠物", price_max_major=Decimal("100"), currency="CNY")

    result = SearchPipeline(FakeEmbedder()).search("宠物", spec, products, limit=10)

    assert result.hits == []
    assert result.empty_reason == "没有商品满足全部条件"


def test_nonempty_result_has_no_empty_reason() -> None:
    products = [_product("P1", "母婴宠物", 500)]
    spec = ProductSearchSpec(category="母婴宠物", currency="CNY")

    result = SearchPipeline(FakeEmbedder()).search("宠物", spec, products, limit=10)

    assert result.hits
    assert result.empty_reason is None
