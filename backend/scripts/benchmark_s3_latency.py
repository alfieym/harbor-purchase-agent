"""Measure search p50 latency: S2 baseline (no rerank) vs S3 (with rerank).

Usage:
    uv run --directory backend python scripts/benchmark_s3_latency.py

Uses the real catalog and a real cross-encoder reranker (default the 0.08 GB
ms-marco smoke model). Embedding is a deterministic local stand-in so the delta
isolates the rerank cost that S3 adds on top of S2.
"""

import os
import statistics
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import QdrantClient

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch
from harbor.features.catalog.search.rerank import FastembedReranker

DATA = Path(__file__).resolve().parents[2] / "data" / "catalog-v1.jsonl"
DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

QUERIES = [
    ("露营灯", ProductSearchSpec(category="户外运动", currency="CNY")),
    ("养猫 轻便 吸尘器", ProductSearchSpec(category="家用电器", currency="CNY")),
    ("母婴 宠物 出行", ProductSearchSpec(category="母婴宠物", currency="CNY")),
    ("轻量 保温杯", ProductSearchSpec(currency="CNY")),
]


class FakeEmbedder:
    """Deterministic character-frequency embedder; no network, no model."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import math

        vectors: list[list[float]] = []
        for text in texts:
            counts = [0.0] * self._dim
            for char in text:
                counts[ord(char) % self._dim] += 1.0
            norm = math.sqrt(sum(value * value for value in counts)) or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


def _build_search(reranker):
    client = QdrantClient(":memory:")
    vectorizer = LexicalVectorizer()
    QdrantProductIndexer(client, FakeEmbedder(), vectorizer=vectorizer).recreate(
        load_products(DATA)
    )
    return QdrantProductSearch(client, FakeEmbedder(), vectorizer=vectorizer, reranker=reranker)


def _p50(search, rounds: int = 5) -> float:
    samples: list[float] = []
    for _ in range(rounds):
        for query, spec in QUERIES:
            result = search.search(query, spec, limit=10)
            samples.append(result.diagnostics.latency_ms)
    return statistics.median(samples)


def main() -> None:
    model = os.environ.get("HARBOR_SEARCH_RERANK_MODEL", DEFAULT_RERANK_MODEL)
    print(f"rerank model: {model}")

    baseline = _p50(_build_search(None))
    with_rerank = _p50(_build_search(FastembedReranker(model)))

    print(f"p50 (S2 baseline, no rerank): {baseline:.1f} ms")
    print(f"p50 (S3, with rerank):       {with_rerank:.1f} ms")
    print(f"rerank overhead:             {with_rerank - baseline:.1f} ms")


if __name__ == "__main__":
    main()
