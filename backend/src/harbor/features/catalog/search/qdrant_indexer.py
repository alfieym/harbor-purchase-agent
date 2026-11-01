"""Index products into Qdrant for semantic + filtered search."""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from qdrant_client import QdrantClient, models

from harbor.features.catalog.models import Product
from harbor.features.catalog.search.embedder import Embedder, product_text
from harbor.features.catalog.search.fx import to_cny
from harbor.features.catalog.search.lexical import LexicalVectorizer

# Fixed namespace turns a product_id into a stable UUID. Local Qdrant only
# accepts UUID point ids, and UUID5 gives a deterministic mapping so re-indexing
# produces the same ids.
_PRODUCT_ID_NAMESPACE = uuid.UUID("7f8d4a2e-3c1b-4d9a-9e6f-2b5a8c0d1e3f")


def point_id_for(product_id: str) -> str:
    return str(uuid.uuid5(_PRODUCT_ID_NAMESPACE, product_id))


def min_price(product: Product) -> tuple[Decimal, str]:
    """Lowest SKU price in the product's single currency (major units)."""
    cheapest = min(product.skus, key=lambda sku: sku.price.amount)
    return cheapest.price.amount, cheapest.price.currency


class QdrantProductIndexer:
    """Builds a product collection from domain ``Product`` objects."""

    def __init__(
        self,
        client: QdrantClient,
        embedder: Embedder,
        collection_name: str = "products",
        vectorizer: LexicalVectorizer | None = None,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection_name = collection_name
        self._vectorizer = vectorizer if vectorizer is not None else LexicalVectorizer()

    def recreate(self, products: Sequence[Product]) -> None:
        """Embed (dense) and vectorize (sparse) all products, dropping any previous collection.

        The fitted ``LexicalVectorizer`` is retained on ``self`` so a search layer
        built against the same instance encodes queries against the same vocab.
        """
        texts = [product_text(product) for product in products]
        self._vectorizer.fit(texts)
        vectors = self._embedder.embed(texts)
        size = len(vectors[0])
        sparse_vectors = [self._vectorizer.encode(text) for text in texts]

        self._client.recreate_collection(
            self._collection_name,
            vectors_config={
                "dense": models.VectorParams(size=size, distance=models.Distance.COSINE)
            },
            # No IDF modifier: weights are tf·idf computed client-side in lexical.py,
            # so Qdrant's sparse dot product matches the reference pipeline exactly.
            sparse_vectors_config={"text": models.SparseVectorParams()},
        )
        points = [
            models.PointStruct(
                id=point_id_for(product.product_id),
                vector={
                    "dense": vector,
                    "text": models.SparseVector(indices=indices, values=values),
                },
                payload=self._payload(product),
            )
            for product, vector, (indices, values) in zip(
                products, vectors, sparse_vectors, strict=True
            )
        ]
        self._client.upsert(self._collection_name, points=points)

    def ensure_indexed(self, products: Sequence[Product]) -> bool:
        """Index ``products`` only if the collection is missing or empty (S3-5).

        Returns True when an index was (re)built, False when it was already populated.
        """
        if self._client.collection_exists(self._collection_name) and self._count() > 0:
            return False
        self.recreate(products)
        return True

    def _count(self) -> int:
        return self._client.count(self._collection_name).count

    @staticmethod
    def _payload(product: Product) -> dict:
        amount, currency = min_price(product)
        return {
            "product_id": product.product_id,
            "title": product.title,
            "category": product.category,
            "tax_category": product.tax_category,
            "brand": product.brand,
            "material_tags": product.material_tags,
            "weight_kg": product.weight_kg,
            "ships_to": product.ships_to,
            "sku_ids": [sku.sku_id for sku in product.skus],
            "min_price_major": float(amount),
            "currency": currency,
            "min_price_cny": float(to_cny(amount, currency)),
            # Full snapshot so the search layer can rebuild the Product without
            # a round-trip to a repository (catalog data is read-only).
            "product": product.model_dump(mode="json"),
        }
