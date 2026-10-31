"""S2-7: camping-lantern regression against the real catalog.

The real catalog has camping lanterns in 户外运动 (e.g. ``CAN-GX-13-*`` and
``CAN-ML-09-*``, cross-currency/cross-language copies). This pins the search on
that data: hard constraints hold, canonical dedup collapses copies, and at least
one lantern comes back.
"""

import math
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.constraints import facts_from_product
from harbor.features.catalog.search.fx import convert
from harbor.features.catalog.search.pipeline import SearchPipeline

DATA = Path(__file__).resolve().parents[2] / "data" / "catalog-v1.jsonl"


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


def test_camping_lantern_search_returns_lanterns_and_dedupes() -> None:
    products = load_products(DATA)
    spec = ProductSearchSpec(category="户外运动", price_max_major=Decimal("200"), currency="CNY")

    result = SearchPipeline(FakeEmbedder()).search("露营灯", spec, products, limit=50)

    assert result.hits  # non-empty

    for hit in result.hits:
        assert hit.product.category == "户外运动"
        facts = facts_from_product(hit.product)
        assert convert(facts.min_price_major, facts.currency, "CNY") <= Decimal("200")

    canonical_ids = [hit.product.canonical_product_id for hit in result.hits]
    assert len(canonical_ids) == len(set(canonical_ids))

    assert any("露营灯" in hit.product.title for hit in result.hits)
