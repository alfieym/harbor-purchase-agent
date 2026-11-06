"""Order domain records shared by the store, service, tools, and HTTP views."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class NewDraftItem:
    """A validated line before the store assigns its line number."""

    product_id: str
    sku_id: str
    title: str
    quantity: int
    unit_price: Decimal
    currency: str
    evidence_unit_price: Decimal
    evidence_currency: str


@dataclass(frozen=True)
class DraftItem:
    """One persisted draft line; prices are already converted and quantized."""

    line_no: int
    product_id: str
    sku_id: str
    title: str
    quantity: int
    unit_price: Decimal
    currency: str
    evidence_unit_price: Decimal
    evidence_currency: str


@dataclass(frozen=True)
class OrderDraft:
    draft_id: str
    buyer_id: str
    shopping_session_id: str
    status: str
    currency: str
    total: Decimal
    items: tuple[DraftItem, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Order:
    order_id: str
    draft_id: str
    buyer_id: str
    shopping_session_id: str
    status: str
    currency: str
    total: Decimal
    items: tuple[DraftItem, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OrderEvent:
    event_id: str
    subject_type: str
    subject_id: str
    event: str
    from_status: str | None
    to_status: str | None
    actor: str
    source: str
    reason: str | None
    metadata: str
    created_at: str


@dataclass(frozen=True)
class CancellationRequest:
    """A buyer's (or app's) request to cancel an order, pending a human decision."""

    request_id: str
    order_id: str
    buyer_id: str
    status: str
    reason: str | None
    idempotency_key: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class SearchEvidence:
    """A price observation for one SKU.

    ``product_id`` is ``None`` when the evidence comes from the S7 compression
    appendix, which protects SKU/title/price but not the product id.
    """

    product_id: str | None
    sku_id: str
    title: str
    unit_price: Decimal
    currency: str
