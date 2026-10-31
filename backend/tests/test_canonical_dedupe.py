"""S2-4: canonical dedup keeps one lowest-price product; hits expose the cheapest SKU."""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

SPEC = ProductSearchSpec(category="家用电器", price_max_major=Decimal("3000"), currency="CNY")


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


def _product(product_id: str, canonical: str, skus: list[tuple[str, float, str]]) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=canonical,
        external_product_id=f"ext-{product_id}",
        title=f"{product_id} 吸尘器 猫毛",
        brand="PureAir",
        category="家用电器",
        tax_category="家用电器",
        description="吸尘器 猫毛",
        highlights=[],
        material_tags=["ABS塑料"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 30, "width": 15, "height": 15},
        weight_kg=1.0,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-S{i}",
                spec=spec,
                price=Money.from_major_units(price_major, currency),
                stock=10,
            )
            for i, (spec, price_major, currency) in enumerate(skus, start=1)
        ],
        updated_at=date(2026, 8, 1),
    )


def test_canonical_duplicates_collapse_to_lowest_price() -> None:
    products = [
        _product("A1", "CAN-SAME", [("标准", 1200, "CNY")]),
        _product("A2", "CAN-SAME", [("促销", 800, "CNY")]),
    ]

    result = SearchPipeline(FakeEmbedder()).search("吸尘器 猫毛", SPEC, products, limit=10)

    assert [hit.product.product_id for hit in result.hits] == ["A2"]


def test_qdrant_dedupes_by_canonical() -> None:
    products = [
        _product("A1", "CAN-SAME", [("标准", 1200, "CNY")]),
        _product("A2", "CAN-SAME", [("促销", 800, "CNY")]),
    ]
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(products)

    result = QdrantProductSearch(client, FakeEmbedder()).search("吸尘器 猫毛", SPEC, limit=10)

    assert [hit.product.product_id for hit in result.hits] == ["A2"]


def test_cross_currency_dedup_picks_lowest_converted() -> None:
    products = [
        _product("A1", "CAN-SAME", [("海外", 100, "USD")]),  # 720 CNY
        _product("A2", "CAN-SAME", [("国行", 900, "CNY")]),  # 900 CNY
    ]

    result = SearchPipeline(FakeEmbedder()).search("吸尘器 猫毛", SPEC, products, limit=10)

    assert [hit.product.product_id for hit in result.hits] == ["A1"]


def test_hit_exposes_lowest_price_sku() -> None:
    products = [_product("V", "CAN-V", [("标准版", 2500, "CNY"), ("促销版", 999, "CNY")])]

    hit = SearchPipeline(FakeEmbedder()).search("吸尘器 猫毛", SPEC, products, limit=10).hits[0]

    assert hit.selected_sku.sku_id == "V-S2"
    assert hit.selected_sku.price.amount == Decimal("999")


def test_pipeline_and_qdrant_agree_after_dedup() -> None:
    products = [
        _product("A1", "CAN-SAME", [("标准", 1200, "CNY")]),
        _product("A2", "CAN-SAME", [("促销", 800, "CNY")]),
        _product("B1", "CAN-OTHER", [("标准", 500, "CNY")]),
    ]

    pipeline_result = SearchPipeline(FakeEmbedder()).search("吸尘器 猫毛", SPEC, products, limit=10)
    pipeline_ids = {hit.product.product_id for hit in pipeline_result.hits}

    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(products)
    qdrant_result = QdrantProductSearch(client, FakeEmbedder()).search(
        "吸尘器 猫毛", SPEC, limit=10
    )
    qdrant_ids = {hit.product.product_id for hit in qdrant_result.hits}

    assert pipeline_ids == qdrant_ids == {"A2", "B1"}
