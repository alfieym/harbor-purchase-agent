"""S2-6: foreign-currency prices convert to the target currency before budget compare.

A product in USD/EUR/JPY/SGD is no longer rejected for ``currency_mismatch``; its
price is converted to the spec's target currency and compared against the budget.
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest
from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.constraints import facts_from_product, matches, reasons_for
from harbor.features.catalog.search.fx import convert, to_cny
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

SPEC = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
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


def _product(product_id: str, price_major: float, currency: str) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
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
                sku_id=f"{product_id}-S1",
                spec="标准",
                price=Money.from_major_units(price_major, currency),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


# --- fx module ---


def test_to_cny_converts_each_currency() -> None:
    assert to_cny(Decimal("100"), "USD") == Decimal("720")
    assert to_cny(Decimal("100"), "CNY") == Decimal("100")
    assert to_cny(Decimal("1000"), "JPY") == Decimal("48")


def test_convert_via_cny_pivot() -> None:
    assert convert(Decimal("100"), "USD", "CNY") == Decimal("720")
    assert convert(Decimal("720"), "CNY", "USD") == Decimal("100")


def test_unknown_currency_raises() -> None:
    with pytest.raises(ValueError):
        to_cny(Decimal("1"), "BTC")


# --- constraint oracle ---


def test_usd_within_budget_matches() -> None:
    assert matches(_product("U1", 100, "USD"), SPEC) is True


def test_usd_over_budget_reports_price_over_not_currency() -> None:
    facts = facts_from_product(_product("U2", 300, "USD"))  # 300 USD = 2160 CNY > 2000
    reasons = reasons_for(facts, SPEC)
    assert [reason.code for reason in reasons] == ["price_over"]
    assert "2160" in reasons[0].detail


def test_same_currency_price_over_keeps_plain_detail() -> None:
    facts = facts_from_product(_product("U3", 2500, "CNY"))
    reasons = reasons_for(facts, SPEC)
    assert [reason.code for reason in reasons] == ["price_over"]
    assert "换算自" not in reasons[0].detail


# --- Qdrant path ---


def test_qdrant_converts_before_budget_compare() -> None:
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(
        [_product("U1", 100, "USD"), _product("U2", 300, "USD")]
    )

    result = QdrantProductSearch(client, FakeEmbedder()).search("吸尘器 猫毛", SPEC, limit=10)

    assert {hit.product.product_id for hit in result.hits} == {"U1"}
    by_id = {candidate.product_id: candidate for candidate in result.filtered_out}
    assert set(by_id) == {"U2"}
    assert [reason.code for reason in by_id["U2"].reasons] == ["price_over"]
