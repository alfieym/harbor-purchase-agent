import json
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.catalog.models import Money
from harbor.features.catalog.persistence.seed_products import load_products


def _raw_product(product_id: str = "P1001") -> dict:
    return {
        "product_id": product_id,
        "canonical_product_id": f"CAN-{product_id}",
        "external_product_id": f"ext-{product_id}",
        "title": "Nomadica 旅行三件套",
        "brand": "Nomadica",
        "category": "旅行装备",
        "tax_category": "旅行装备",
        "description": "帆布加尼龙材质，结实耐磨",
        "highlights": [{"label": "材质", "detail": "帆布+再生尼龙"}],
        "material_tags": ["合成聚合物"],
        "origin_country": "VN",
        "ships_to": ["CN", "US"],
        "source_platform": "amazon",
        "dimensions_cm": {"length": 12, "width": 8, "height": 4},
        "weight_kg": 0.15,
        "rating_summary": {"average": 4.0, "review_count": 30},
        "skus": [
            {
                "sku_id": f"{product_id}-S1",
                "spec": "军绿色",
                "price_major": 189.0,
                "currency": "CNY",
                "stock": 50,
            }
        ],
        "updated_at": "2026-08-01",
        "evaluation_family": "M1",
    }


def _write_jsonl(path: Path, records: list[object]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_load_products_maps_raw_skus(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    _write_jsonl(path, [_raw_product()])

    products = load_products(path)

    assert len(products) == 1
    assert products[0].product_id == "P1001"
    assert products[0].skus[0].price == Money(amount=Decimal("189.0"), currency="CNY")
    assert "evaluation_family" not in products[0].model_dump()


def test_load_products_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_raw_product("P1"), ensure_ascii=False),
                "",
                json.dumps(_raw_product("P2"), ensure_ascii=False),
                "",
            ]
        ),
        encoding="utf-8",
    )

    products = load_products(path)

    assert [product.product_id for product in products] == ["P1", "P2"]


def test_load_products_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r":1: invalid JSON"):
        load_products(path)


def test_load_products_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r":1: expected a JSON object"):
        load_products(path)


def test_load_products_wraps_invalid_record(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    record = _raw_product()
    record.pop("title")
    _write_jsonl(path, [record])

    with pytest.raises(ValueError, match=r":1: invalid product record"):
        load_products(path)


def test_load_products_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "catalog-v1.jsonl"
    path.write_text("", encoding="utf-8")

    assert load_products(path) == []
