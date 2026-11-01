"""S3: degradation — rerank failure keeps recall order; embedding timeout falls back."""

import math
import time
from collections.abc import Sequence
from datetime import date

import httpx
from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.pipeline import SearchPipeline
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


class TimeoutEmbedder:
    """Raises the same exception httpx raises when the vector service times out."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise httpx.TimeoutException("vector service timed out")


class ExplodingReranker:
    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        raise RuntimeError("reranker down")


def _product(product_id: str, title: str, category: str = "母婴宠物") -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=title,
        brand="TestBrand",
        category=category,
        tax_category=category,
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


def test_rerank_failure_keeps_recall_order_and_reports_failed() -> None:
    products = [_product("A", "无线吸尘器"), _product("B", "养猫用品")]
    spec = ProductSearchSpec(category="母婴宠物")

    plain = SearchPipeline(FakeEmbedder()).search("养猫", spec, products, limit=10)
    broken = SearchPipeline(FakeEmbedder(), reranker=ExplodingReranker()).search(
        "养猫", spec, products, limit=10
    )

    assert [hit.product.product_id for hit in broken.hits] == [
        hit.product.product_id for hit in plain.hits
    ]
    assert broken.diagnostics.rerank_status == "failed"


def test_embedding_timeout_falls_back_within_budget() -> None:
    products = [_product("A", "无线吸尘器"), _product("B", "养猫用品")]
    spec = ProductSearchSpec(category="母婴宠物")
    client = QdrantClient(":memory:")
    vectorizer = LexicalVectorizer()
    QdrantProductIndexer(client, FakeEmbedder(), vectorizer=vectorizer).recreate(products)

    start = time.perf_counter()
    result = QdrantProductSearch(client, TimeoutEmbedder(), vectorizer=vectorizer).search(
        "养猫", spec, limit=10
    )
    elapsed = (time.perf_counter() - start) * 1000

    assert result.recall_strategy == "keyword_2gram"
    assert [hit.product.product_id for hit in result.hits] == ["B"]
    # The fallback is local and fast — well within any "timeout + 1s" budget.
    assert elapsed < 1000
