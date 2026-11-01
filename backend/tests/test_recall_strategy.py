"""S3: the recall fallback chain — embedding, keyword_2gram, and exact_id_lookup."""

import math
from collections.abc import Sequence
from datetime import date

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.embedder import product_text
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch


class DictEmbedder:
    """Maps known texts to fixed vectors; unknown texts get the zero vector."""

    def __init__(self, vectors: dict[str, list[float]], dim: int) -> None:
        self._vectors = vectors
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


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


class FailingEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")


class CountingEmbedder:
    """Returns a fixed vector but records every embed call, to prove it is never invoked."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * self._dim for _ in texts]


def _product(
    product_id: str, title: str, category: str = "家用电器", sku_id: str = "S1"
) -> Product:
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
                sku_id=f"{product_id}-{sku_id}",
                spec="标准",
                price=Money.from_major_units(100, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_embedding_recalls_paraphrase_in_pipeline() -> None:
    products = [_product("A", "无线吸尘器"), _product("B", "台灯")]
    embedder = DictEmbedder(
        {
            "打扫猫毛的机器": [1.0, 0.0],
            product_text(products[0]): [1.0, 0.0],
            product_text(products[1]): [0.0, 1.0],
        },
        dim=2,
    )

    result = SearchPipeline(embedder).search(
        "打扫猫毛的机器", ProductSearchSpec(category="家用电器"), products, limit=10
    )

    assert result.recall_strategy == "embedding"
    assert result.hits[0].product.product_id == "A"


def test_embedding_failure_falls_back_to_keyword_2gram_in_qdrant() -> None:
    products = [_product("A", "无线吸尘器", "母婴宠物"), _product("B", "养猫用品", "母婴宠物")]
    spec = ProductSearchSpec(category="母婴宠物")
    client = QdrantClient(":memory:")
    vectorizer = LexicalVectorizer()
    QdrantProductIndexer(client, FakeEmbedder(), vectorizer=vectorizer).recreate(products)

    result = QdrantProductSearch(client, FailingEmbedder(), vectorizer=vectorizer).search(
        "养猫", spec, limit=10
    )

    assert result.recall_strategy == "keyword_2gram"
    assert [hit.product.product_id for hit in result.hits] == ["B"]


def test_exact_sku_id_lookup_skips_embedding_in_qdrant() -> None:
    products = [_product("P1", "无线吸尘器", sku_id="SKU-123")]
    spec = ProductSearchSpec(category="家用电器")
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, FakeEmbedder()).recreate(products)

    counting = CountingEmbedder()
    result = QdrantProductSearch(client, counting).search("P1-SKU-123", spec, limit=10)

    assert result.recall_strategy == "exact_id_lookup"
    assert [hit.product.product_id for hit in result.hits] == ["P1"]
    assert counting.calls == 0


def test_exact_sku_id_lookup_skips_embedding_in_pipeline() -> None:
    products = [_product("P1", "无线吸尘器", sku_id="SKU-123")]
    spec = ProductSearchSpec(category="家用电器")

    counting = CountingEmbedder()
    result = SearchPipeline(counting).search("P1-SKU-123", spec, products, limit=10)

    assert result.recall_strategy == "exact_id_lookup"
    assert [hit.product.product_id for hit in result.hits] == ["P1"]
    assert counting.calls == 0
