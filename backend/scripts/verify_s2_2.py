"""Manually verify S2-2: rejected candidates carry reasons instead of vanishing.

Usage:
    uv run --directory backend python scripts/verify_s2_2.py [--limit N] [--pool N]

Prints the hits, the rejected candidates with their reasons, and the reason
counts, then self-checks the S2-2 invariants.
"""

import argparse
import math
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from qdrant_client import QdrantClient

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.constraints import (
    facts_from_product,
    matches,
    reasons_for,
)
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "catalog-vacuum-demo.jsonl"

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="number of hits to keep")
    parser.add_argument("--pool", type=int, default=100, help="unfiltered candidate window size")
    args = parser.parse_args()

    products = load_products(FIXTURE)
    embedder = FakeEmbedder()
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, embedder).recreate(products)
    search = QdrantProductSearch(client, embedder, candidate_pool_size=args.pool)
    result = search.search(QUERY, SPEC, limit=args.limit)

    print(f"query: {QUERY}")
    print(
        f"spec:  category={SPEC.category}  price<={SPEC.price_max_major} {SPEC.currency}"
        f"  weight<={SPEC.weight_max_kg}kg  excluded={SPEC.excluded_material_tags}"
    )
    print(f"data:  {len(products)} products   pool={args.pool}   limit={args.limit}")

    print(f"\nhits ({len(result.hits)}):")
    for hit in result.hits:
        print(f"  {hit.product.product_id:6} score={hit.score:.4f}  {hit.product.title}")

    print(f"\nfiltered_out ({len(result.filtered_out)}):")
    for candidate in result.filtered_out:
        print(f"  {candidate.product_id:6} score={candidate.score:.4f}  {candidate.title}")
        for reason in candidate.reasons:
            print(f"         [{reason.code}] {reason.detail}")

    print(f"\nreason_counts: {result.reason_counts}")

    problems: list[str] = []
    for hit in result.hits:
        if not matches(hit.product, SPEC):
            problems.append(f"hit {hit.product.product_id} violates the spec")
    for candidate in result.filtered_out:
        if not candidate.reasons:
            problems.append(f"filtered_out {candidate.product_id} has no reason")
    for product in products:
        facts = facts_from_product(product)
        if (reasons_for(facts, SPEC) == []) != matches(product, SPEC):
            problems.append(f"oracle mismatch for {product.product_id}")

    print(f"\nS2-2: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")


if __name__ == "__main__":
    main()
