"""Manually verify S2-4: canonical dedup + lowest-price SKU.

Two checks:
1. Data side: every multi-product canonical group satisfies the semantic contract
   (brand / category / material equal), so dedup by canonical id is safe.
2. Code side: the reference pipeline collapses a same-canonical group to its
   lowest-price member (converted to the target currency) and exposes the
   lowest-price SKU on each hit.

Usage:
    uv run --directory backend python scripts/verify_s2_4.py
"""

import collections
import json
import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.pipeline import SearchPipeline

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "data" / "catalog-v1.jsonl"

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


def _material_key(product: dict) -> tuple[str, ...]:
    return tuple(product.get("material_tags") or [])


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


def check_data_side() -> list[str]:
    rows = [
        json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        groups[row["canonical_product_id"]].append(row)
    multi = {cid: members for cid, members in groups.items() if len(members) > 1}
    singles = {cid for cid, members in groups.items() if len(members) == 1}

    violations = [
        cid
        for cid, members in multi.items()
        if not (
            len({member["brand"] for member in members}) == 1
            and len({member["category"] for member in members}) == 1
            and len({_material_key(member) for member in members}) == 1
        )
    ]

    print(f"data:     {DATA}")
    print(f"records:  {len(rows)}")
    print(f"groups:   {len(groups)}  = {len(singles)} singletons + {len(multi)} multi-member")
    print(f"contract: {len(violations)} violating multi-member groups")

    problems: list[str] = []
    if len(rows) != 3700:
        problems.append(f"record count {len(rows)} != 3700")
    if len(groups) != 725:
        problems.append(f"group count {len(groups)} != 725")
    if violations:
        problems.append(f"{len(violations)} violating groups: {violations[:5]}")
    return problems


def check_code_side() -> list[str]:
    products = [
        _product("A1", "CAN-SAME", [("标准", 1200, "CNY")]),
        _product("A2", "CAN-SAME", [("促销", 800, "CNY")]),
        _product("A3", "CAN-SAME", [("海外", 100, "USD")]),  # 720 CNY
    ]
    result = SearchPipeline(FakeEmbedder()).search("吸尘器 猫毛", SPEC, products, limit=10)

    print("\ndedupe (A1=1200CNY / A2=800CNY / A3=100USD→720CNY, same canonical):")
    for hit in result.hits:
        sku = hit.selected_sku
        print(
            f"  {hit.product.product_id:4} lowest SKU {sku.sku_id} "
            f"{sku.price.amount} {sku.price.currency}"
        )

    ids = [hit.product.product_id for hit in result.hits]
    if ids != ["A3"]:
        return [f"expected only A3 (720 CNY lowest), got {ids}"]
    return []


def main() -> None:
    data_problems = check_data_side()
    print(f"\nS2-4 data side:   {'OK' if not data_problems else 'FAIL'}")
    for problem in data_problems:
        print(f"  - {problem}")

    code_problems = check_code_side()
    print(f"S2-4 code side:   {'OK' if not code_problems else 'FAIL'}")
    for problem in code_problems:
        print(f"  - {problem}")


if __name__ == "__main__":
    main()
