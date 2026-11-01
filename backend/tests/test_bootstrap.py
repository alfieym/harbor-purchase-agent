"""S3-5: an empty index is bootstrapped on demand instead of returning empty."""

import math
from collections.abc import Sequence
from datetime import date

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch


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


def _product(product_id: str, title: str) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=title,
        brand="TestBrand",
        category="家用电器",
        tax_category="家用电器",
        description="",
        highlights=[],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=0.5,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-S1",
                spec="标准",
                price=Money.from_major_units(100, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_empty_index_is_bootstrapped_on_demand() -> None:
    client = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    vectorizer = LexicalVectorizer()
    indexer = QdrantProductIndexer(client, embedder, vectorizer=vectorizer)
    products = [_product("P1", "吸尘器")]

    # No collection yet → bootstrap builds it.
    assert indexer.ensure_indexed(products) is True

    # Search now returns the product, not an empty result.
    result = QdrantProductSearch(client, embedder, vectorizer=vectorizer).search(
        "吸尘器", ProductSearchSpec(category="家用电器"), limit=10
    )
    assert [hit.product.product_id for hit in result.hits] == ["P1"]

    # A second call is a no-op: the index is already populated.
    assert indexer.ensure_indexed(products) is False
