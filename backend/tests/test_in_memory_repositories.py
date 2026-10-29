from datetime import date
from decimal import Decimal

import pytest

from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.persistence.in_memory_repositories import InMemoryProductRepository


def _product(product_id: str) -> Product:
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
        skus=[
            Sku(
                sku_id=f"{product_id}-S1",
                spec="standard",
                price=Money(amount=Decimal("1.00"), currency="CNY"),
                stock=1,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_find_by_id() -> None:
    first = _product("P1")
    second = _product("P2")
    repository = InMemoryProductRepository([first, second])

    assert repository.find_by_id("P1") == first
    assert repository.find_by_id("missing") is None


def test_find_by_ids_preserves_order_and_skips_missing() -> None:
    repository = InMemoryProductRepository([_product("P1"), _product("P2")])

    products = repository.find_by_ids(["P2", "missing", "P1"])

    assert [product.product_id for product in products] == ["P2", "P1"]


def test_list_all_returns_all_products_in_insertion_order() -> None:
    repository = InMemoryProductRepository([_product("P2"), _product("P1")])

    assert [product.product_id for product in repository.list_all()] == ["P2", "P1"]


def test_duplicate_product_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate product_id: P1"):
        InMemoryProductRepository([_product("P1"), _product("P1")])


def test_empty_repository() -> None:
    repository = InMemoryProductRepository()

    assert repository.find_by_id("P1") is None
    assert repository.find_by_ids(["P1"]) == []
    assert repository.list_all() == []
