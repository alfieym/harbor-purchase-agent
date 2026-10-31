"""Manually verify S2-6: foreign-currency prices convert to the target before budget compare.

Usage:
    uv run --directory backend python scripts/verify_s2_6.py
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.fx import CNY_PER_UNIT
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


def main() -> None:
    print("FX rates (CNY per unit):", {k: str(v) for k, v in CNY_PER_UNIT.items()})
    print(
        f"\nspec: category=家用电器  price<={SPEC.price_max_major} {SPEC.currency}"
        f"  weight<={SPEC.weight_max_kg}"
    )

    products = [
        _product("U1", 100, "USD"),  # 720 CNY  -> 预算内 -> 命中
        _product("U2", 300, "USD"),  # 2160 CNY -> 超预算 -> price_over
        _product("U3", 2500, "CNY"),  # 同币种超预算
        _product("U4", 500, "CNY"),  # 预算内
    ]
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(products)
    result = QdrantProductSearch(client, FakeEmbedder()).search("吸尘器 猫毛", SPEC, limit=10)

    print(f"\nhits ({len(result.hits)}):")
    for hit in result.hits:
        print(f"  {hit.product.product_id:6} score={hit.score:.4f}")
    print(f"\nfiltered_out ({len(result.filtered_out)}):")
    for candidate in result.filtered_out:
        print(f"  {candidate.product_id:6}")
        for reason in candidate.reasons:
            print(f"         [{reason.code}] {reason.detail}")

    hit_ids = {hit.product.product_id for hit in result.hits}
    out_ids = {candidate.product_id for candidate in result.filtered_out}
    problems: list[str] = []
    if hit_ids != {"U1", "U4"}:
        problems.append(f"expected hits U1+U4, got {hit_ids}")
    if out_ids != {"U2", "U3"}:
        problems.append(f"expected filtered U2+U3, got {out_ids}")
    u2 = next(c for c in result.filtered_out if c.product_id == "U2")
    if [reason.code for reason in u2.reasons] != ["price_over"]:
        problems.append("U2 reason should be [price_over], not currency_mismatch")

    print(f"\nS2-6: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")


if __name__ == "__main__":
    main()
