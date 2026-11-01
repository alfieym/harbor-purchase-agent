"""Cross-encoder reranking with graceful degradation (S3).

Rerank is an optional quality layer: it re-scores the top candidates semantically
but never decides whether a candidate is a hit — hard constraints do. When the
reranker is disabled or fails, callers keep the recall order, so a broken reranker
degrades to "no rerank" rather than failing the search.
"""

import logging
from collections.abc import Sequence
from typing import Protocol

from harbor.features.catalog.models import Product
from harbor.features.catalog.search.embedder import product_text
from harbor.features.catalog.search.schemas import RerankStatus

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    """Scores each document's relevance to a query; higher is more relevant."""

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]: ...


class FastembedReranker:
    """Local cross-encoder reranker on fastembed's ONNX runtime (no torch).

    The model is downloaded from HuggingFace on first use and cached. Model choice
    is configurable: ``Xenova/ms-marco-MiniLM-L-6-v2`` (0.08 GB) smokes the
    pipeline, ``BAAI/bge-reranker-base`` (1.04 GB, EN/ZH) buys production quality.
    """

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(self._model_name, cache_dir=self._cache_dir)
        return self._model

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        model = self._load()
        # fastembed returns one score per document, in document order.
        return list(model.rerank(query, list(documents)))


def apply_rerank(
    reranker: Reranker | None,
    query: str,
    scored: Sequence[tuple[Product, float]],
    top_k: int,
) -> tuple[list[tuple[Product, float]], RerankStatus]:
    """Re-score the top ``top_k`` candidates; the rest keep their order.

    Returns ``(scored, status)`` where ``status`` reports what the rerank stage
    did: ``disabled`` when no reranker is configured, ``failed`` when a configured
    reranker raised (order unchanged), ``success`` otherwise.
    """
    if reranker is None or top_k <= 0:
        return list(scored), "disabled"
    head = list(scored[:top_k])
    tail = list(scored[top_k:])
    if not head:
        return list(scored), "disabled"
    try:
        scores = reranker.rerank(query, [product_text(product) for product, _ in head])
    except Exception:  # noqa: BLE001 — degrade rather than fail the search
        logger.warning("rerank failed; keeping recall order", exc_info=True)
        return list(scored), "failed"
    reranked = sorted(
        ((product, float(score)) for (product, _), score in zip(head, scores, strict=True)),
        key=lambda item: (-item[1], item[0].product_id),
    )
    return reranked + tail, "success"
