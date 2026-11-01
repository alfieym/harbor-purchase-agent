"""Transparent reference pipeline for catalog search (fallback-chain recall, S3).

``QdrantProductSearch`` runs the same recall fallback chain in Qdrant; this module
spells it out in Python so each stage can be inspected, taught and tested.

   exact_id_lookup → 打分(embedding) → 过滤 → 精排 → 去重 → 截断 → 组装
                     （embedding 失败时降级为 keyword_2gram 打分）

It is a reference oracle and teaching aid, not the production hot path (which
stays on Qdrant): both paths must agree on the same inputs, which the tests check.
``sort`` remains as the dense-ordering helper used by tests to inspect stage 1.
"""

import logging
import time
from collections.abc import Sequence

from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.constraints import (
    empty_reason_for,
    facts_from_product,
    matches,
    min_sku,
    reasons_for,
)
from harbor.features.catalog.search.dedupe import dedupe_by_canonical
from harbor.features.catalog.search.embedder import Embedder, product_text
from harbor.features.catalog.search.lexical import LexicalVectorizer, sparse_dot
from harbor.features.catalog.search.rerank import Reranker, apply_rerank
from harbor.features.catalog.search.schemas import (
    RecallStrategy,
    RejectedCandidate,
    RerankStatus,
    SearchDiagnostics,
    SearchHit,
    SearchResult,
)

# A candidate paired with its similarity score (stage 1 output).
ScoredProduct = tuple[Product, float]

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_POOL_SIZE = 100
DEFAULT_RERANK_TOP_K = 30


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


class SearchPipeline:
    """Runs the fallback-chain recall and exposes each stage as an inspectable method."""

    def __init__(
        self,
        embedder: Embedder,
        reranker: Reranker | None = None,
        rerank_top_k: int = DEFAULT_RERANK_TOP_K,
    ) -> None:
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_top_k = rerank_top_k

    def score(self, query_text: str, products: Sequence[Product]) -> list[ScoredProduct]:
        """Stage — 打分(embedding): dual-tower dense cosine score per candidate."""
        query_vector = self._embedder.embed([query_text])[0]
        vectors = self._embedder.embed([product_text(product) for product in products])
        return [
            (product, cosine(query_vector, vector))
            for product, vector in zip(products, vectors, strict=True)
        ]

    @staticmethod
    def lexical_score(query_text: str, products: Sequence[Product]) -> list[ScoredProduct]:
        """Stage — 打分(keyword_2gram): sparse bigram dot, dropping non-overlapping products."""
        vectorizer = LexicalVectorizer().fit([product_text(product) for product in products])
        query_sparse = vectorizer.encode(query_text)
        result: list[ScoredProduct] = []
        for product in products:
            dot = sparse_dot(query_sparse, vectorizer.encode(product_text(product)))
            if dot > 0:
                result.append((product, dot))
        return result

    @staticmethod
    def sort(scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Ordering helper: score descending, product_id breaks ties."""
        return sorted(scored, key=lambda item: (-item[1], item[0].product_id))

    @staticmethod
    def filter(spec: ProductSearchSpec, scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Stage — 过滤: drop candidates that fail any hard constraint."""
        return [(product, score) for product, score in scored if matches(product, spec)]

    def rerank(
        self, query_text: str, scored: Sequence[ScoredProduct]
    ) -> tuple[list[ScoredProduct], RerankStatus]:
        """Stage — 精排: re-score the top candidates; reports what the stage did."""
        return apply_rerank(self._reranker, query_text, scored, self._rerank_top_k)

    @staticmethod
    def dedupe(spec: ProductSearchSpec, scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Stage — 去重: keep one lowest-price product per canonical id."""
        return dedupe_by_canonical(scored, spec)

    @staticmethod
    def truncate(sorted_: Sequence[ScoredProduct], limit: int) -> list[ScoredProduct]:
        """Stage — 截断: keep only the top ``limit``."""
        return list(sorted_[:limit])

    @staticmethod
    def assemble(scored: Sequence[ScoredProduct]) -> list[SearchHit]:
        """Stage — 组装: build the final result objects."""
        return [
            SearchHit(product=product, score=score, selected_sku=min_sku(product))
            for product, score in scored
        ]

    @staticmethod
    def exact_lookup(query_text: str, products: Sequence[Product]) -> list[ScoredProduct] | None:
        """Fast path: return the product whose id equals the query, without embedding."""
        for product in products:
            if product.product_id == query_text or any(
                sku.sku_id == query_text for sku in product.skus
            ):
                return [(product, 1.0)]
        return None

    def search(
        self,
        query_text: str,
        spec: ProductSearchSpec,
        products: Sequence[Product],
        limit: int = 10,
        candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    ) -> SearchResult:
        """Run the fallback chain and report what hard constraints rejected."""
        start = time.perf_counter()
        window = max(candidate_pool_size, limit)

        exact = self.exact_lookup(query_text, products)
        if exact is not None:
            strategy: RecallStrategy = "exact_id_lookup"
            ranked = [(product, score) for product, score in exact if matches(product, spec)]
            unfiltered = exact
        else:
            dense = self._score_or_none(query_text, products)
            if dense is not None:
                strategy = "embedding"
                ranked = self.sort(self.filter(spec, dense))
                unfiltered = self.sort(dense)
            else:
                strategy = "keyword_2gram"
                sparse = self.lexical_score(query_text, products)
                ranked = self.sort(self.filter(spec, sparse))
                unfiltered = self.sort(sparse)

        reranked, rerank_status = self.rerank(query_text, ranked)
        hits = self.assemble(self.truncate(self.dedupe(spec, reranked), limit))

        pool = unfiltered[:window]
        returned = {hit.product.product_id for hit in hits}
        filtered_out: list[RejectedCandidate] = []
        reason_counts: dict[str, int] = {}
        for product, score in pool:
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

        empty_reason = None
        if not hits:
            category_exists = True
            if spec.category is not None:
                category_exists = any(product.category == spec.category for product in products)
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

    def _score_or_none(
        self, query_text: str, products: Sequence[Product]
    ) -> list[ScoredProduct] | None:
        """Dense score, degrading to ``None`` (keyword_2gram) on embedding failure."""
        try:
            return self.score(query_text, products)
        except Exception:  # noqa: BLE001 — degrade rather than fail the search
            logger.warning("embedding failed; falling back to keyword_2gram", exc_info=True)
            return None
