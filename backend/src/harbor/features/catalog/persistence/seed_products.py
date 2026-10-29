"""Load versioned catalog JSONL into domain objects."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harbor.features.catalog.models import Money, Product, Sku


def load_products(path: Path) -> list[Product]:
    """Load a versioned catalog JSONL file into domain ``Product`` objects.

    The version is encoded in the path (for example ``catalog-v1.jsonl``);
    this loader itself is version-agnostic.
    """
    products: list[Product] = []

    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc

            if not isinstance(record, Mapping):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")

            try:
                products.append(_to_product(record))
            except (KeyError, TypeError, ValidationError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid product record") from exc

    return products


def _to_product(record: Mapping[str, Any]) -> Product:
    payload = dict(record)
    payload["skus"] = [_to_sku(sku) for sku in record["skus"]]
    return Product.model_validate(payload)


def _to_sku(raw: Mapping[str, Any]) -> Sku:
    return Sku(
        sku_id=raw["sku_id"],
        spec=raw["spec"],
        price=Money(amount=raw["price_major"], currency=raw["currency"]),
        stock=raw["stock"],
    )
