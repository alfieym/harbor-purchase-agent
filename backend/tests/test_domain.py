from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.persistence.in_memory_repositories import InMemoryProductRepository


def _sku(stock: int = 5) -> Sku:
    return Sku(
        sku_id="P1-S1",
        spec="标准",
        price=Money.from_major_units(189.0, "CNY"),
        stock=stock,
    )


def _product(product_id: str, skus: list[Sku] | None = None) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=f"Product {product_id}",
        brand="Brand",
        category="Category",
        tax_category="Category",
        description="Description",
        highlights=[],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 1, "width": 1, "height": 1},
        weight_kg=1.0,
        rating_summary={"average": 4.0, "review_count": 1},
        skus=[_sku()] if skus is None else skus,
        updated_at=date(2026, 8, 1),
    )


def test_money_major_and_minor_units() -> None:
    money = Money.from_major_units(189.0, "CNY")

    assert isinstance(money.amount, Decimal)
    assert money.amount == Decimal("189.0")
    assert money.amount_in_minor_units == 18900


def test_money_rejects_negative_amount_and_invalid_currency() -> None:
    with pytest.raises(ValueError):
        Money.from_major_units(-1, "CNY")

    with pytest.raises(ValueError):
        Money.from_major_units(1, "cny")


def test_sku_stock_deduction_and_restock() -> None:
    sku = _sku(stock=5)

    sku.deduct_stock(3)
    assert sku.stock == 2

    sku.restock(3)
    assert sku.stock == 5

    with pytest.raises(ValueError, match="insufficient stock"):
        sku.deduct_stock(6)

    assert sku.stock == 5


def test_product_requires_at_least_one_sku() -> None:
    with pytest.raises(ValidationError, match="at least one Sku"):
        _product("P1", [])


def test_in_memory_repository_lookup() -> None:
    first = _product("P1")
    second = _product("P2")
    repo = InMemoryProductRepository([first, second])

    assert repo.find_by_id("P1") == first
    assert repo.find_by_id("missing") is None
    assert [product.product_id for product in repo.find_by_ids(["P2", "missing"])] == ["P2"]
    assert [product.product_id for product in repo.list_all()] == ["P1", "P2"]
