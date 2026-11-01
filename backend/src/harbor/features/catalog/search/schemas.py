"""Search result DTOs."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from harbor.features.catalog.models import Product, Sku

# Which recall strategy produced the hits (S3). A fallback chain, not a fusion:
# exact_id_lookup → embedding → keyword_2gram.
RecallStrategy = Literal["exact_id_lookup", "embedding", "keyword_2gram"]

# What the rerank stage did (S3). "failed" when a configured reranker raised,
# "disabled" when none was configured.
RerankStatus = Literal["success", "failed", "disabled"]


class SearchDiagnostics(BaseModel):
    """Runtime observability for a single search (S3)."""

    rerank_status: RerankStatus = "disabled"
    latency_ms: float = 0.0


class SearchHit(BaseModel):
    """A single search hit: the product, its score, and its lowest-price SKU."""

    product: Product
    score: float
    selected_sku: Sku


class CandidateFacts(BaseModel):
    """The fields hard constraints are evaluated against, without the full product.

    Keeping this projection small lets rejected candidates travel (and later be
    re-evaluated against a new spec) without shipping a whole product snapshot.
    """

    product_id: str
    title: str
    min_price_major: Decimal
    currency: str
    weight_kg: float
    category: str
    material_tags: list[str]


class RejectReason(BaseModel):
    """One hard constraint a candidate failed."""

    code: str
    detail: str


class RejectedCandidate(CandidateFacts):
    """A candidate excluded by hard constraints, with every reason why."""

    score: float
    reasons: list[RejectReason]


class SearchResult(BaseModel):
    """Hits plus the candidates that hard constraints rejected and runtime diagnostics."""

    hits: list[SearchHit]
    filtered_out: list[RejectedCandidate] = Field(default_factory=list)
    reason_counts: dict[str, int] = Field(default_factory=dict)
    empty_reason: str | None = None
    recall_strategy: RecallStrategy = "embedding"
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)
