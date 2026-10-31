"""Semantic search with hard-constraint filtering backed by Qdrant."""

from decimal import Decimal

from qdrant_client import QdrantClient, models

from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.constraints import empty_reason_for, min_sku, reasons_for
from harbor.features.catalog.search.dedupe import dedupe_by_canonical
from harbor.features.catalog.search.embedder import Embedder
from harbor.features.catalog.search.fx import to_cny
from harbor.features.catalog.search.schemas import (
    CandidateFacts,
    RejectedCandidate,
    SearchHit,
    SearchResult,
)

DEFAULT_CANDIDATE_POOL_SIZE = 100


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


def _facts_from_payload(payload: dict) -> CandidateFacts:
    """Rebuild constraint facts from a payload, without deserializing the product."""
    return CandidateFacts(
        product_id=payload["product_id"],
        title=payload.get("title", ""),
        min_price_major=Decimal(str(payload["min_price_major"])),
        currency=payload["currency"],
        weight_kg=payload["weight_kg"],
        category=payload["category"],
        material_tags=list(payload.get("material_tags") or []),
    )


class QdrantProductSearch:
    """Runs a semantic query against hard constraints and returns hits."""

    def __init__(
        self,
        client: QdrantClient,
        embedder: Embedder,
        collection_name: str = "products",
        candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection_name = collection_name
        self._candidate_pool_size = candidate_pool_size

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

    def search(
        self,
        query_text: str,
        spec: ProductSearchSpec,
        limit: int = 10,
    ) -> SearchResult:
        vector = self._embedder.embed([query_text])[0]

        # Query 1 — filtered: fetch a window of compliant candidates, then dedup
        # by canonical id before truncating (S2-4).
        hit_response = self._client.query_points(
            self._collection_name,
            query=vector,
            query_filter=build_filter(spec),
            limit=max(self._candidate_pool_size, limit),
        )
        scored: list[tuple[Product, float]] = []
        for point in hit_response.points:
            payload = point.payload or {}
            scored.append((Product.model_validate(payload["product"]), point.score))
        deduped = dedupe_by_canonical(scored, spec)[:limit]
        hits: list[SearchHit] = [
            SearchHit(product=product, score=score, selected_sku=min_sku(product))
            for product, score in deduped
        ]

        # Query 2 — unfiltered window: the nearest candidates, so the ones the
        # filter dropped can be reported instead of silently disappearing.
        pool_response = self._client.query_points(
            self._collection_name,
            query=vector,
            limit=max(self._candidate_pool_size, limit),
        )
        returned = {hit.product.product_id for hit in hits}
        filtered_out: list[RejectedCandidate] = []
        reason_counts: dict[str, int] = {}
        for point in pool_response.points:
            payload = point.payload or {}
            if payload["product_id"] in returned:
                continue
            facts = _facts_from_payload(payload)
            reasons = reasons_for(facts, spec)
            if not reasons:
                continue
            filtered_out.append(
                RejectedCandidate(score=point.score, reasons=reasons, **facts.model_dump())
            )
            for reason in reasons:
                reason_counts[reason.code] = reason_counts.get(reason.code, 0) + 1

        empty_reason = None
        if not hits:
            category_exists = True
            if spec.category is not None:
                category_exists = self._category_exists(spec.category)
            empty_reason = empty_reason_for(spec, category_exists)

        return SearchResult(
            hits=hits,
            filtered_out=filtered_out,
            reason_counts=reason_counts,
            empty_reason=empty_reason,
        )
