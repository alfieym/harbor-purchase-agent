"""S10: application-layer order endpoints (``/commerce/orders``).

These routes are the human gate. Nothing here is reachable by the model: the
buyer's app calls confirm to turn a draft into an order, and confirms again to
turn a cancellation request into a cancelled order. Every response carries an
``idempotent`` flag so a double-click or a retried request is harmless.

Route order matters: the static ``/drafts`` paths are declared before
``/{order_id}`` so the parameterised route never swallows them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from harbor.features.orders.schemas import (
    BuyerRequest,
    CancelDraftResponse,
    CancellationCreateRequest,
    CancellationView,
    ConfirmCancellationResponse,
    ConfirmDraftResponse,
    OrderDetailView,
    OrderDraftView,
    OrderView,
    RequestCancellationResponse,
    WithdrawCancellationResponse,
)
from harbor.features.orders.service import (
    OrderConflict,
    OrderNotFound,
    OrderService,
    OrderServiceError,
)

router = APIRouter(prefix="/orders", tags=["orders"])


def _service(request: Request) -> OrderService:
    service: OrderService | None = getattr(request.app.state, "order_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="订单服务未配置")
    return service


def _http_error(exc: OrderServiceError) -> HTTPException:
    if isinstance(exc, OrderNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OrderConflict):
        return HTTPException(
            status_code=409,
            detail={"message": str(exc), "current_status": exc.current_status},
        )
    return HTTPException(status_code=500, detail="订单服务内部错误")


@router.get("/drafts", response_model=OrderDraftView | None)
async def pending_draft(
    buyer_id: str, shopping_session_id: str, request: Request
) -> OrderDraftView | None:
    """The page-reload path (spec §6.2): the pending draft, or ``null``."""
    draft = await _service(request).get_pending_draft(
        buyer_id=buyer_id, shopping_session_id=shopping_session_id
    )
    return OrderDraftView.from_draft(draft) if draft is not None else None


@router.post("/drafts/{draft_id}/confirm", response_model=ConfirmDraftResponse)
async def confirm_draft(
    draft_id: str, body: BuyerRequest, request: Request
) -> ConfirmDraftResponse:
    try:
        order, idempotent = await _service(request).confirm_draft(
            draft_id=draft_id, buyer_id=body.buyer_id
        )
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return ConfirmDraftResponse(order=OrderView.from_order(order), idempotent=idempotent)


@router.post("/drafts/{draft_id}/cancel", response_model=CancelDraftResponse)
async def cancel_draft(draft_id: str, body: BuyerRequest, request: Request) -> CancelDraftResponse:
    try:
        draft, _ = await _service(request).cancel_draft(draft_id=draft_id, buyer_id=body.buyer_id)
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return CancelDraftResponse(draft=OrderDraftView.from_draft(draft))


@router.get("/{order_id}", response_model=OrderDetailView)
async def get_order(order_id: str, buyer_id: str, request: Request) -> OrderDetailView:
    try:
        order, cancellation = await _service(request).get_order(
            order_id=order_id, buyer_id=buyer_id
        )
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return OrderDetailView(
        order=OrderView.from_order(order),
        cancellation=CancellationView.from_request(cancellation) if cancellation else None,
    )


@router.post("/{order_id}/cancellation", response_model=RequestCancellationResponse)
async def request_cancellation(
    order_id: str, body: CancellationCreateRequest, request: Request
) -> RequestCancellationResponse:
    try:
        cancellation, idempotent = await _service(request).request_cancellation(
            order_id=order_id,
            buyer_id=body.buyer_id,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
        )
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return RequestCancellationResponse(
        cancellation=CancellationView.from_request(cancellation), idempotent=idempotent
    )


@router.post("/{order_id}/cancellation/confirm", response_model=ConfirmCancellationResponse)
async def confirm_cancellation(
    order_id: str, body: BuyerRequest, request: Request
) -> ConfirmCancellationResponse:
    try:
        order, cancellation, idempotent = await _service(request).confirm_cancellation(
            order_id=order_id, buyer_id=body.buyer_id
        )
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return ConfirmCancellationResponse(
        order=OrderView.from_order(order),
        cancellation=CancellationView.from_request(cancellation),
        idempotent=idempotent,
    )


@router.post("/{order_id}/cancellation/withdraw", response_model=WithdrawCancellationResponse)
async def withdraw_cancellation(
    order_id: str, body: BuyerRequest, request: Request
) -> WithdrawCancellationResponse:
    try:
        cancellation, _ = await _service(request).withdraw_cancellation(
            order_id=order_id, buyer_id=body.buyer_id
        )
    except OrderServiceError as exc:
        raise _http_error(exc) from exc
    return WithdrawCancellationResponse(cancellation=CancellationView.from_request(cancellation))
