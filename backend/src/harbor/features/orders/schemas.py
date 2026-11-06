"""S10: HTTP views for drafts, orders, and cancellation requests."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from harbor.features.orders.models import (
    CancellationRequest,
    DraftItem,
    Order,
    OrderDraft,
)


class OrderItemView(BaseModel):
    product_id: str
    sku_id: str
    title: str
    quantity: int
    unit_price: Decimal
    currency: str

    @classmethod
    def from_item(cls, item: DraftItem) -> OrderItemView:
        return cls(
            product_id=item.product_id,
            sku_id=item.sku_id,
            title=item.title,
            quantity=item.quantity,
            unit_price=item.unit_price,
            currency=item.currency,
        )


class OrderDraftView(BaseModel):
    draft_id: str
    status: str
    currency: str
    total: Decimal
    items: list[OrderItemView]
    created_at: str
    updated_at: str

    @classmethod
    def from_draft(cls, draft: OrderDraft) -> OrderDraftView:
        return cls(
            draft_id=draft.draft_id,
            status=draft.status,
            currency=draft.currency,
            total=draft.total,
            items=[OrderItemView.from_item(item) for item in draft.items],
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )


class OrderView(BaseModel):
    order_id: str
    draft_id: str
    status: str
    currency: str
    total: Decimal
    items: list[OrderItemView]
    created_at: str
    updated_at: str

    @classmethod
    def from_order(cls, order: Order) -> OrderView:
        return cls(
            order_id=order.order_id,
            draft_id=order.draft_id,
            status=order.status,
            currency=order.currency,
            total=order.total,
            items=[OrderItemView.from_item(item) for item in order.items],
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class CancellationView(BaseModel):
    request_id: str
    status: str
    reason: str | None
    created_at: str
    decided_at: str | None

    @classmethod
    def from_request(cls, request: CancellationRequest) -> CancellationView:
        return cls(
            request_id=request.request_id,
            status=request.status,
            reason=request.reason,
            created_at=request.created_at,
            decided_at=request.decided_at,
        )


class OrderDetailView(BaseModel):
    order: OrderView
    cancellation: CancellationView | None = None


class BuyerRequest(BaseModel):
    buyer_id: str


class CancellationCreateRequest(BaseModel):
    buyer_id: str
    reason: str | None = None
    idempotency_key: str


class ConfirmDraftResponse(BaseModel):
    order: OrderView
    idempotent: bool


class CancelDraftResponse(BaseModel):
    draft: OrderDraftView


class RequestCancellationResponse(BaseModel):
    cancellation: CancellationView
    idempotent: bool


class ConfirmCancellationResponse(BaseModel):
    order: OrderView
    cancellation: CancellationView
    idempotent: bool


class WithdrawCancellationResponse(BaseModel):
    cancellation: CancellationView
