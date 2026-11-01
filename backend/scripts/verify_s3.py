"""Manually verify S3: fallback-chain recall + cross-encoder rerank + degradation.

Usage:
    uv run --directory backend python scripts/verify_s3.py

The reranker is a real fastembed cross-encoder. The default model is the 0.08 GB
``Xenova/ms-marco-MiniLM-L-6-v2`` (fast smoke, English-only). Set
``HARBOR_SEARCH_RERANK_MODEL=BAAI/bge-reranker-base`` (1.04 GB, EN/ZH) to buy the
Chinese quality that matters in production.
"""

import os
from collections.abc import Sequence
from datetime import date

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.embedder import product_text
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch
from harbor.features.catalog.search.rerank import FastembedReranker

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
SPEC = ProductSearchSpec(category="家用电器")


class DictEmbedder:
    """Hand-places known texts on a dense space so a paraphrase maps to its product."""

    def __init__(self, vectors: dict[str, list[float]], dim: int) -> None:
        self._vectors = vectors
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


class FailingEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")


def _product(product_id: str, title: str, sku_id: str = "S1") -> Product:
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
                sku_id=f"{product_id}-{sku_id}",
                spec="标准",
                price=Money.from_major_units(100, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def _products() -> list[Product]:
    return [_product("A", "无线吸尘器"), _product("B", "台灯", sku_id="LAMP-7")]


def _embedder() -> DictEmbedder:
    products = _products()
    return DictEmbedder(
        {
            "打扫猫毛的机器": [1.0, 0.0],  # paraphrase of 吸尘器
            product_text(products[0]): [1.0, 0.0],
            product_text(products[1]): [0.0, 1.0],
        },
        dim=2,
    )


def _search(embedder, vectorizer, reranker=None) -> QdrantProductSearch:
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, _embedder(), vectorizer=vectorizer).recreate(_products())
    return QdrantProductSearch(client, embedder, vectorizer=vectorizer, reranker=reranker)


def main() -> None:
    model = os.environ.get("HARBOR_SEARCH_RERANK_MODEL", DEFAULT_RERANK_MODEL)
    print(f"rerank model: {model}")

    problems: list[str] = []

    # 1. embedding: a paraphrase recalls its product via the dual-tower embedding.
    vectorizer = LexicalVectorizer()
    result = _search(_embedder(), vectorizer).search("打扫猫毛的机器", SPEC, limit=10)
    print(
        f"1. paraphrase → {[h.product.product_id for h in result.hits]} "
        f"(strategy={result.recall_strategy}, rerank={result.diagnostics.rerank_status})"
    )
    if result.hits[0].product.product_id != "A" or result.recall_strategy != "embedding":
        problems.append("paraphrase recall should use embedding and return A")

    # 2. real cross-encoder rerank runs and reports success.
    reranker = FastembedReranker(model)
    vectorizer2 = LexicalVectorizer()
    result = _search(_embedder(), vectorizer2, reranker).search("打扫猫毛的机器", SPEC, limit=10)
    print(
        f"2. +rerank → {[(h.product.product_id, round(h.score, 4)) for h in result.hits]} "
        f"(rerank={result.diagnostics.rerank_status})"
    )
    if result.diagnostics.rerank_status != "success":
        problems.append("rerank should report success")

    # 3. embedding down → keyword_2gram fallback.
    vectorizer3 = LexicalVectorizer()
    result = _search(FailingEmbedder(), vectorizer3).search("台灯", SPEC, limit=10)
    print(
        f"3. embedding down → {[h.product.product_id for h in result.hits]} "
        f"(strategy={result.recall_strategy})"
    )
    if result.recall_strategy != "keyword_2gram":
        problems.append("embedding failure should fall back to keyword_2gram")

    # 4. exact sku_id → exact_id_lookup, no embedding.
    vectorizer4 = LexicalVectorizer()
    result = _search(FailingEmbedder(), vectorizer4).search("B-LAMP-7", SPEC, limit=10)
    print(
        f"4. exact sku_id → {[h.product.product_id for h in result.hits]} "
        f"(strategy={result.recall_strategy})"
    )
    if result.recall_strategy != "exact_id_lookup" or result.hits[0].product.product_id != "B":
        problems.append("exact sku_id should use exact_id_lookup and return B")

    print(f"\nS3: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")


if __name__ == "__main__":
    main()
