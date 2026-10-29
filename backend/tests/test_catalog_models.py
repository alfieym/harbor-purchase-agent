from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku

BASE_FIELDS = {
    "product_id",
    "canonical_product_id",
    "external_product_id",
    "title",
    "brand",
    "category",
    "tax_category",
    "description",
    "highlights",
    "material_tags",
    "origin_country",
    "ships_to",
    "source_platform",
    "dimensions_cm",
    "weight_kg",
    "rating_summary",
    "skus",
    "updated_at",
}

OPTIONAL_METADATA = {
    "data_provenance",
    "evaluation_family",
    "evaluation_tier",
    "evaluation_tags",
    "model_spec",
    "language_group",
    "source_language",
    "source_locale",
    "localized_category",
    "localized_material",
}

RAW_PRODUCT = {
    "product_id": "P1001",
    "canonical_product_id": "CAN-LEGACY-000",
    "external_product_id": "amazon-P1001",
    "title": "Nomadica 旅行三件套",
    "brand": "Nomadica",
    "category": "旅行装备",
    "tax_category": "旅行装备",
    "description": "帆布加尼龙材质，结实耐磨",
    "highlights": [
        {"label": "材质", "detail": "帆布+再生尼龙"},
        {"label": "重量", "detail": "420g"},
    ],
    "material_tags": ["合成聚合物"],
    "origin_country": "VN",
    "ships_to": ["CN", "US", "SG"],
    "source_platform": "amazon",
    "dimensions_cm": {"length": 12, "width": 8, "height": 4},
    "weight_kg": 0.15,
    "rating_summary": {"average": 4.0, "review_count": 30},
    "skus": [
        {
            "sku_id": "P1001-S1",
            "spec": "军绿色",
            "price_major": 189.0,
            "currency": "CNY",
            "stock": 50,
        },
        {
            "sku_id": "P1001-S2",
            "spec": "沙漠黄",
            "price_major": 199.0,
            "currency": "CNY",
            "stock": 30,
        },
    ],
    "updated_at": "2026-08-01",
    "data_provenance": {"source": "synthetic"},
    "evaluation_family": "M1",
    "evaluation_tier": 2,
    "evaluation_tags": ["budget"],
    "model_spec": {"feature": "capacity", "value": 20, "unit": "L"},
    "language_group": "zh",
    "source_language": "zh",
    "source_locale": "zh-CN",
    "localized_category": "旅行装备",
    "localized_material": "合成聚合物",
}


def _map_sku(raw: dict) -> Sku:
    return Sku(
        sku_id=raw["sku_id"],
        spec=raw["spec"],
        price=Money(amount=raw["price_major"], currency=raw["currency"]),
        stock=raw["stock"],
    )


def _map_product(raw: dict) -> Product:
    return Product.model_validate({**raw, "skus": [_map_sku(sku) for sku in raw["skus"]]})


def _sku(stock: int = 5) -> Sku:
    return Sku(
        sku_id="P1001-S1",
        spec="军绿色",
        price=Money(amount=Decimal("189.0"), currency="CNY"),
        stock=stock,
    )


@pytest.mark.parametrize("amount", [Decimal("189.00"), 189, 189.0])
def test_money_accepts_decimal_int_and_float(amount: Decimal | int | float) -> None:
    money = Money(amount=amount, currency="CNY")

    assert isinstance(money.amount, Decimal)
    assert money.amount == Decimal("189.00")


def test_money_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-1"), currency="CNY")


@pytest.mark.parametrize("currency", ["cny", "CN", "CNYY", "123", ""])
def test_money_rejects_invalid_currency(currency: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"), currency=currency)


def test_money_is_frozen() -> None:
    money = Money(amount=Decimal("1"), currency="CNY")

    with pytest.raises(ValidationError):
        money.amount = Decimal("2")


@pytest.mark.parametrize("amount", [Decimal("-1"), -1, "-0.01"])
def test_money_from_major_units_rejects_negative_amount(
    amount: Decimal | int | str,
) -> None:
    with pytest.raises(ValueError):
        Money.from_major_units(amount, "CNY")


@pytest.mark.parametrize("currency", ["cny", "CN", "CNYY", "123", ""])
def test_money_from_major_units_rejects_invalid_currency(currency: str) -> None:
    with pytest.raises(ValueError):
        Money.from_major_units(1, currency)


def test_money_from_major_units_to_minor_units() -> None:
    money = Money.from_major_units(189.0, "CNY")

    assert money == Money(amount=Decimal("189.0"), currency="CNY")
    assert money.amount_in_minor_units == 18900


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("189.00"), "CNY", 18900),
        (0.1, "CNY", 10),
        (1.23, "USD", 123),
        (189, "JPY", 189),
        (1.23, "GBP", 123),
    ],
)
def test_money_minor_units_for_currencies(
    amount: Decimal | int | float,
    currency: str,
    expected: int,
) -> None:
    money = Money.from_major_units(amount, currency)

    assert money.amount_in_minor_units == expected


def test_money_rejects_fractional_minor_units() -> None:
    money = Money.from_major_units(Decimal("189.999"), "CNY")

    with pytest.raises(ValueError, match="more than 2 fractional digits"):
        _ = money.amount_in_minor_units


def test_sku_maps_raw_price_fields() -> None:
    sku = _map_sku(RAW_PRODUCT["skus"][0])

    assert sku.price == Money(amount=Decimal("189.0"), currency="CNY")
    assert isinstance(sku.price.amount, Decimal)


def test_sku_allows_zero_stock() -> None:
    sku = Sku(
        sku_id="P1001-S1",
        spec="军绿色",
        price=Money(amount=Decimal("189.0"), currency="CNY"),
        stock=0,
    )

    assert sku.stock == 0


def test_sku_rejects_negative_stock() -> None:
    with pytest.raises(ValidationError):
        Sku(
            sku_id="P1001-S1",
            spec="军绿色",
            price=Money(amount=Decimal("189.0"), currency="CNY"),
            stock=-1,
        )


def test_sku_deduct_stock() -> None:
    sku = _sku(stock=5)

    sku.deduct_stock(3)

    assert sku.stock == 2


def test_sku_deduct_stock_rejects_insufficient_stock() -> None:
    sku = _sku(stock=5)

    with pytest.raises(ValueError, match="insufficient stock: requested 6, available 5"):
        sku.deduct_stock(6)

    assert sku.stock == 5


def test_sku_deduct_stock_rejects_negative_quantity() -> None:
    sku = _sku(stock=5)

    with pytest.raises(ValueError, match="quantity must be non-negative"):
        sku.deduct_stock(-1)

    assert sku.stock == 5


def test_sku_restock_restores_stock() -> None:
    sku = _sku(stock=5)

    sku.deduct_stock(3)
    sku.restock(3)

    assert sku.stock == 5


def test_sku_rejects_negative_restock() -> None:
    sku = _sku(stock=5)

    with pytest.raises(ValueError, match="quantity must be non-negative"):
        sku.restock(-1)

    assert sku.stock == 5


def test_sku_rejects_negative_stock_assignment() -> None:
    sku = _sku(stock=5)

    with pytest.raises(ValidationError):
        sku.stock = -1

    assert sku.stock == 5


def test_sku_requires_nested_price() -> None:
    with pytest.raises(ValidationError):
        Sku(
            sku_id="P1001-S1",
            spec="军绿色",
            price_major=189.0,
            currency="CNY",
            stock=1,
        )


def test_product_maps_representative_record() -> None:
    product = _map_product(RAW_PRODUCT)

    assert product.product_id == "P1001"
    assert product.canonical_product_id == "CAN-LEGACY-000"
    assert product.brand == "Nomadica"
    assert product.category == "旅行装备"
    assert product.origin_country == "VN"
    assert product.ships_to == ["CN", "US", "SG"]
    assert product.updated_at == date(2026, 8, 1)
    assert len(product.skus) == 2
    assert product.skus[0].price == Money(amount=Decimal("189.0"), currency="CNY")


def test_product_keeps_raw_nested_fields() -> None:
    product = _map_product(RAW_PRODUCT)

    assert product.dimensions_cm == RAW_PRODUCT["dimensions_cm"]
    assert product.highlights == RAW_PRODUCT["highlights"]
    assert product.rating_summary == RAW_PRODUCT["rating_summary"]
    assert isinstance(product.rating_summary["review_count"], int)


def test_product_ignores_evaluation_metadata() -> None:
    dumped = _map_product(RAW_PRODUCT).model_dump()

    assert set(dumped) == BASE_FIELDS
    assert not (set(dumped) & OPTIONAL_METADATA)


def test_product_accepts_single_sku() -> None:
    product = _map_product({**RAW_PRODUCT, "skus": [RAW_PRODUCT["skus"][0]]})

    assert len(product.skus) == 1


def test_product_rejects_empty_skus() -> None:
    with pytest.raises(ValidationError, match="at least one Sku"):
        _map_product({**RAW_PRODUCT, "skus": []})


@pytest.mark.parametrize("weight", [0.0, -1.0])
def test_product_rejects_non_positive_weight(weight: float) -> None:
    with pytest.raises(ValidationError):
        _map_product({**RAW_PRODUCT, "weight_kg": weight})


def test_product_rejects_invalid_updated_at() -> None:
    with pytest.raises(ValidationError):
        _map_product({**RAW_PRODUCT, "updated_at": "2026-13-01"})


def test_product_requires_all_top_level_fields() -> None:
    raw = dict(RAW_PRODUCT)
    raw.pop("title")

    with pytest.raises(ValidationError):
        _map_product(raw)


def test_product_rejects_non_numeric_dimensions() -> None:
    with pytest.raises(ValidationError):
        _map_product(
            {
                **RAW_PRODUCT,
                "dimensions_cm": {"length": "tall", "width": 8, "height": 4},
            }
        )


def test_search_spec_defaults() -> None:
    spec = ProductSearchSpec()

    assert spec.normalized_query == ""
    assert spec.price_max_major is None
    assert spec.currency == "CNY"
    assert spec.excluded_material_tags == []


def test_search_spec_full_spec_coerces_decimal() -> None:
    spec = ProductSearchSpec(
        normalized_query="backpack",
        price_max_major=300.5,
        currency="USD",
        excluded_material_tags=["皮革"],
    )

    assert spec.price_max_major == Decimal("300.5")
    assert isinstance(spec.price_max_major, Decimal)
    assert spec.currency == "USD"


def test_search_spec_rejects_negative_price() -> None:
    with pytest.raises(ValidationError):
        ProductSearchSpec(price_max_major=Decimal("-0.01"))


def test_search_spec_rejects_invalid_currency() -> None:
    with pytest.raises(ValidationError):
        ProductSearchSpec(currency="cny")


def test_search_spec_lists_are_independent() -> None:
    first = ProductSearchSpec()
    second = ProductSearchSpec()

    assert first.excluded_material_tags is not second.excluded_material_tags
