"""Search result DTOs."""

from decimal import Decimal

from pydantic import BaseModel, Field

from harbor.features.catalog.models import Product, Sku


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
    """Hits plus the candidates that hard constraints rejected."""

    hits: list[SearchHit]
    filtered_out: list[RejectedCandidate] = Field(default_factory=list)
    reason_counts: dict[str, int] = Field(default_factory=dict)
    empty_reason: str | None = None
