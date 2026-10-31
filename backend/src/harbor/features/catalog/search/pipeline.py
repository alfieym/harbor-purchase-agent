"""Transparent six-stage reference pipeline for catalog search.

``QdrantProductSearch`` compresses scoring / filtering / sorting / truncation
into a single ``query_points`` call; this module spells the same six stages out
in Python so each stage's effect can be inspected, taught and tested.

   打分(score) → 过滤(filter) → 排序(sort) → 去重(dedupe) → 截断(truncate) → 组装(assemble)

It is a reference oracle and teaching aid, not the production hot path (which
stays on Qdrant): both paths must agree on the same inputs, which the tests
``test_search_pipeline.py`` and ``test_rejected_reasons.py`` check.
"""

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
from harbor.features.catalog.search.schemas import (
    RejectedCandidate,
    SearchHit,
    SearchResult,
)

# A candidate paired with its similarity score (stage 1 output).
ScoredProduct = tuple[Product, float]

DEFAULT_CANDIDATE_POOL_SIZE = 100


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


class SearchPipeline:
    """Runs the six stages and exposes each as an inspectable method."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def score(self, query_text: str, products: Sequence[Product]) -> list[ScoredProduct]:
        """Stage 1 — 打分: score every candidate, no filtering yet."""
        query_vector = self._embedder.embed([query_text])[0]
        vectors = self._embedder.embed([product_text(product) for product in products])
        return [
            (product, cosine(query_vector, vector))
            for product, vector in zip(products, vectors, strict=True)
        ]

    @staticmethod
    def filter(spec: ProductSearchSpec, scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Stage 2 — 过滤: drop candidates that fail any hard constraint."""
        return [(product, score) for product, score in scored if matches(product, spec)]

    @staticmethod
    def sort(scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Stage 3 — 排序: descending score; product_id breaks ties deterministically."""
        return sorted(scored, key=lambda item: (-item[1], item[0].product_id))

    @staticmethod
    def dedupe(spec: ProductSearchSpec, scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        """Stage 4 — 去重: keep one lowest-price product per canonical id."""
        return dedupe_by_canonical(scored, spec)

    @staticmethod
    def truncate(sorted_: Sequence[ScoredProduct], limit: int) -> list[ScoredProduct]:
        """Stage 5 — 截断: keep only the top ``limit``."""
        return list(sorted_[:limit])

    @staticmethod
    def assemble(scored: Sequence[ScoredProduct]) -> list[SearchHit]:
        """Stage 6 — 组装: build the final result objects."""
        return [
            SearchHit(product=product, score=score, selected_sku=min_sku(product))
            for product, score in scored
        ]

    def search(
        self,
        query_text: str,
        spec: ProductSearchSpec,
        products: Sequence[Product],
        limit: int = 10,
        candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    ) -> SearchResult:
        """Chain the six stages and report what hard constraints rejected.

        The rejected set mirrors ``QdrantProductSearch``: the top
        ``candidate_pool_size`` candidates by raw similarity (no filter applied),
        minus the ones returned as hits, each explained by ``reasons_for``.
        """
        scored = self.score(query_text, products)

        hits = self.assemble(
            self.truncate(self.dedupe(spec, self.sort(self.filter(spec, scored))), limit)
        )

        pool = self.sort(scored)[: max(candidate_pool_size, limit)]
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

        return SearchResult(
            hits=hits,
            filtered_out=filtered_out,
            reason_counts=reason_counts,
            empty_reason=empty_reason,
        )
