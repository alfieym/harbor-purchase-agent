"""Fallback-chain recall with hard constraints, rerank and diagnostics (S3).

Recall is a fallback chain, not a fusion:

    exact_id_lookup  — the query is a known product_id / sku_id → return it directly,
                       without touching the embedding service at all.
    embedding        — dual-tower semantic recall (dense vector).
    keyword_2gram    — CJK bigram lexical recall, used only when embedding fails.

The result reports which strategy fired (``recall_strategy``) and what the rerank
stage did (``diagnostics.rerank_status``).
"""

import logging
import time

from qdrant_client import QdrantClient, models

from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.constraints import (
    empty_reason_for,
    facts_from_product,
    matches,
    min_sku,
    reasons_for,
)
from harbor.features.catalog.search.dedupe import dedupe_by_canonical
from harbor.features.catalog.search.embedder import Embedder
from harbor.features.catalog.search.fx import to_cny
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.qdrant_indexer import point_id_for
from harbor.features.catalog.search.rerank import Reranker, apply_rerank
from harbor.features.catalog.search.schemas import (
    RecallStrategy,
    RejectedCandidate,
    SearchDiagnostics,
    SearchHit,
    SearchResult,
)

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_POOL_SIZE = 100
DEFAULT_RERANK_TOP_K = 30


def build_filter(spec: ProductSearchSpec) -> models.Filter:
    """Translate a ``ProductSearchSpec`` into Qdrant hard-constraint conditions.

    Every condition lives under ``must`` (or ``must_not``), so Qdrant enforces
    them during retrieval: a hit either satisfies all hard constraints or is not
    returned at all. Vector similarity only orders the survivors.
    """
    must: list[models.Condition] = []
    if spec.price_max_major is not None:
        budget_cny = float(to_cny(spec.price_max_major, spec.currency))
        must.append(
            models.FieldCondition(
                key="min_price_cny",
                range=models.Range(lte=budget_cny),
            )
        )
    if spec.weight_max_kg is not None:
        must.append(
            models.FieldCondition(key="weight_kg", range=models.Range(lte=spec.weight_max_kg))
        )
    if spec.category is not None:
        must.append(
            models.FieldCondition(key="category", match=models.MatchValue(value=spec.category))
        )

    must_not: list[models.Condition] = [
        models.FieldCondition(key="material_tags", match=models.MatchValue(value=tag))
        for tag in spec.excluded_material_tags
    ]
    if must_not:
        return models.Filter(must=must, must_not=must_not)
    return models.Filter(must=must)


class QdrantProductSearch:
    """Runs the fallback-chain recall against hard constraints and returns hits."""

    def __init__(
        self,
        client: QdrantClient,
        embedder: Embedder,
        collection_name: str = "products",
        candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
        vectorizer: LexicalVectorizer | None = None,
        reranker: Reranker | None = None,
        rerank_top_k: int = DEFAULT_RERANK_TOP_K,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection_name = collection_name
        self._candidate_pool_size = candidate_pool_size
        self._vectorizer = vectorizer if vectorizer is not None else LexicalVectorizer()
        self._reranker = reranker
        self._rerank_top_k = rerank_top_k

    def _category_exists(self, category: str) -> bool:
        """Whether any indexed point carries ``category``."""
        response = self._client.count(
            self._collection_name,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(key="category", match=models.MatchValue(value=category))
                ]
            ),
            exact=True,
        )
        return response.count > 0

    def _embed_query(self, query_text: str) -> list[float] | None:
        """Embed the query, degrading to ``None`` (keyword_2gram) on failure or timeout."""
        try:
            return self._embedder.embed([query_text])[0]
        except Exception:  # noqa: BLE001 — degrade rather than fail the search
            logger.warning("embedding failed; falling back to keyword_2gram", exc_info=True)
            return None

    def _find_by_id(self, query: str) -> Product | None:
        """Resolve ``query`` as a product_id or sku_id, without any embedding."""
        points = self._client.retrieve(
            self._collection_name, ids=[point_id_for(query)], with_payload=True
        )
        if points:
            return Product.model_validate((points[0].payload or {})["product"])
        points, _ = self._client.scroll(
            self._collection_name,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="sku_ids", match=models.MatchValue(value=query))]
            ),
            limit=1,
            with_payload=True,
        )
        if points:
            return Product.model_validate((points[0].payload or {})["product"])
        return None

    def _exact_lookup(self, query: str) -> list[tuple[Product, float]] | None:
        """Return the exact-id hit, or ``None`` when ``query`` is not an id."""
        product = self._find_by_id(query)
        if product is None:
            return None
        return [(product, 1.0)]

    def _dense_rank(
        self, vector: list[float], query_filter: models.Filter | None, limit: int
    ) -> list[tuple[Product, float]]:
        response = self._client.query_points(
            self._collection_name,
            query=vector,
            using="dense",
            query_filter=query_filter,
            limit=limit,
        )
        return [
            (Product.model_validate((point.payload or {})["product"]), point.score)
            for point in response.points
        ]

    def _sparse_rank(
        self, query_text: str, query_filter: models.Filter | None, limit: int
    ) -> list[tuple[Product, float]]:
        indices, values = self._vectorizer.encode(query_text)
        if not indices:
            return []
        response = self._client.query_points(
            self._collection_name,
            query=models.SparseVector(indices=indices, values=values),
            using="text",
            query_filter=query_filter,
            limit=limit,
        )
        return [
            (Product.model_validate((point.payload or {})["product"]), point.score)
            for point in response.points
            if point.score > 0
        ]

    @staticmethod
    def _rejected(
        unfiltered: list[tuple[Product, float]], returned: set[str], spec: ProductSearchSpec
    ) -> tuple[list[RejectedCandidate], dict[str, int]]:
        """Build ``filtered_out`` / ``reason_counts`` from the unfiltered pool."""
        filtered_out: list[RejectedCandidate] = []
        reason_counts: dict[str, int] = {}
        for product, score in unfiltered:
            if product.product_id in returned:
                continue
            facts = facts_from_product(product)
            reasons = reasons_for(facts, spec)
            if not reasons:
                continue
            filtered_out.append(
                RejectedCandidate(score=score, reasons=reasons, **facts.model_dump())
            )
            for reason in reasons:
                reason_counts[reason.code] = reason_counts.get(reason.code, 0) + 1
        return filtered_out, reason_counts

    def search(
        self,
        query_text: str,
        spec: ProductSearchSpec,
        limit: int = 10,
    ) -> SearchResult:
        start = time.perf_counter()
        window = max(self._candidate_pool_size, limit)

        # Fallback chain: exact_id_lookup → embedding → keyword_2gram.
        exact = self._exact_lookup(query_text)
        if exact is not None:
            strategy: RecallStrategy = "exact_id_lookup"
            ranked = [(product, score) for product, score in exact if matches(product, spec)]
            unfiltered = exact
        else:
            vector = self._embed_query(query_text)
            if vector is not None:
                strategy = "embedding"
                ranked = self._dense_rank(vector, build_filter(spec), window)
                unfiltered = self._dense_rank(vector, None, window)
            else:
                strategy = "keyword_2gram"
                ranked = self._sparse_rank(query_text, build_filter(spec), window)
                unfiltered = self._sparse_rank(query_text, None, window)

        reranked, rerank_status = apply_rerank(
            self._reranker, query_text, ranked, self._rerank_top_k
        )
        deduped = dedupe_by_canonical(reranked, spec)[:limit]
        hits = [
            SearchHit(product=product, score=score, selected_sku=min_sku(product))
            for product, score in deduped
        ]

        returned = {hit.product.product_id for hit in hits}
        filtered_out, reason_counts = self._rejected(unfiltered, returned, spec)

        empty_reason = None
        if not hits:
            category_exists = True
            if spec.category is not None:
                category_exists = self._category_exists(spec.category)
            empty_reason = empty_reason_for(spec, category_exists)

        latency_ms = (time.perf_counter() - start) * 1000
        return SearchResult(
            hits=hits,
            filtered_out=filtered_out,
            reason_counts=reason_counts,
            empty_reason=empty_reason,
            recall_strategy=strategy,
            diagnostics=SearchDiagnostics(rerank_status=rerank_status, latency_ms=latency_ms),
        )
