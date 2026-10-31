"""S2-3: hard constraints are applied before sorting and truncation.

The fixture alone cannot prove this: its top-scoring product (V102) is already
compliant, so "filter then sort" and "sort then filter" return the same thing.
These tests add a product that scores highest *and* is over budget, which makes
the two orderings diverge.
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.constraints import matches
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"

QUERY = "养猫 轻便 吸尘器"

SPEC = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
    excluded_material_tags=["过敏原"],
)


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


def _over_budget_top_scorer() -> Product:
    """Text closest to the query, but far over the price ceiling."""
    title = "养猫 轻便 吸尘器 猫毛 轻量 无线 吸尘器"
    return Product(
        product_id="X-OVER",
        canonical_product_id="CAN-X-OVER",
        external_product_id="ext-X-OVER",
        title=title,
        brand="PureAir",
        category="家用电器",
        tax_category="家用电器",
        description=title,
        highlights=[],
        material_tags=["ABS塑料"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="demo",
        dimensions_cm={"length": 30, "width": 15, "height": 15},
        weight_kg=1.0,
        rating_summary={"average": 4.9, "review_count": 999},
        skus=[
            Sku(
                sku_id="X-OVER-S1",
                spec="标准",
                price=Money.from_major_units(9999, "CNY"),
                stock=1,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def _products() -> list:
    return load_products(FIXTURE) + [_over_budget_top_scorer()]


def test_over_budget_top_scorer_is_not_hits_zero() -> None:
    products = _products()
    pipeline = SearchPipeline(FakeEmbedder())

    # Premise: X-OVER really is the highest-scoring candidate.
    ranked = pipeline.sort(pipeline.score(QUERY, products))
    assert ranked[0][0].product_id == "X-OVER"

    # With limit=1, filter-then-truncate gives the slot to the best compliant
    # product, not to the top scorer.
    result = pipeline.search(QUERY, SPEC, products, limit=1)

    assert result.hits[0].product.product_id == "V102"
    over = next(c for c in result.filtered_out if c.product_id == "X-OVER")
    assert [reason.code for reason in over.reasons] == ["price_over"]


def test_qdrant_also_filters_before_truncating() -> None:
    products = _products()
    embedder = FakeEmbedder()
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, embedder).recreate(products)

    result = QdrantProductSearch(client, embedder).search(QUERY, SPEC, limit=1)

    assert result.hits[0].product.product_id == "V102"
    assert "X-OVER" in {candidate.product_id for candidate in result.filtered_out}


def test_scenario_discriminates_the_two_orderings() -> None:
    """Prove the setup can tell the orderings apart — otherwise the test is idle."""
    products = _products()
    pipeline = SearchPipeline(FakeEmbedder())
    ranked = pipeline.sort(pipeline.score(QUERY, products))

    # The wrong order (truncate first, then filter) returns nothing at limit=1.
    truncate_then_filter = [
        product for product, _ in pipeline.truncate(ranked, limit=1) if matches(product, SPEC)
    ]

    assert truncate_then_filter == []
    assert pipeline.search(QUERY, SPEC, products, limit=1).hits[0].product.product_id == "V102"
