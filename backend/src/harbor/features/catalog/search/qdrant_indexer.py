"""Index products into Qdrant for semantic + filtered search."""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from qdrant_client import QdrantClient, models

from harbor.features.catalog.models import Product
from harbor.features.catalog.search.embedder import Embedder, product_text
from harbor.features.catalog.search.fx import to_cny

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
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection_name = collection_name

    def recreate(self, products: Sequence[Product]) -> None:
        """Embed and upsert all products, dropping any previous collection."""
        texts = [product_text(product) for product in products]
        vectors = self._embedder.embed(texts)
        size = len(vectors[0])

        self._client.recreate_collection(
            self._collection_name,
            vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
        )
        points = [
            models.PointStruct(
                id=point_id_for(product.product_id),
                vector=vector,
                payload=self._payload(product),
            )
            for product, vector in zip(products, vectors, strict=True)
        ]
        self._client.upsert(self._collection_name, points=points)

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
            "min_price_major": float(amount),
            "currency": currency,
            "min_price_cny": float(to_cny(amount, currency)),
            # Full snapshot so the search layer can rebuild the Product without
            # a round-trip to a repository (catalog data is read-only).
            "product": product.model_dump(mode="json"),
        }
