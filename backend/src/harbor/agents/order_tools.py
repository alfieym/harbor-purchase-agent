"""S10: the two transaction tools the model may call.

``create_order_draft`` is the write chain's only model-visible entry point: it
records what to buy, never an order. ``get_order`` is read-only. Confirming a
draft or cancelling an order happens through the application layer with the
buyer's own action, so neither tool accepts a "confirm" argument — a model that
hallucinates a purchase still cannot create one.

Every argument is optional at the schema level. A missing or mistyped argument
must become ``ok=false`` in a SUCCESS tool result, never a framework validation
error: ``run_turn`` treats any error tool result in the turn as a 502, and a
model fumbling an argument should get a correction, not an outage.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from agentscope.message import TextBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk
from pydantic import BaseModel, Field, ValidationError

from harbor.agents.context_governance import PROTECTED_FACTS_HEADER, facts_from_appendix
from harbor.agents.grounding import search_evidence_from_context
from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.features.orders.models import (
    CancellationRequest,
    Order,
    OrderDraft,
    SearchEvidence,
)
from harbor.features.orders.service import (
    DraftRequestItem,
    OrderNotFound,
    OrderService,
    OrderStorageError,
    OrderValidationError,
)

logger = logging.getLogger(__name__)


class DraftItemArgs(BaseModel):
    """One requested line. All fields optional so the framework never rejects."""

    product_id: str | None = Field(default=None, description="商品 ID（来自 product_search 命中）")
    sku_id: str | None = Field(default=None, description="SKU ID（来自 product_search 命中）")
    quantity: int | None = Field(default=None, description="数量，1–99，默认 1")


class CreateOrderDraftArgs(BaseModel):
    items: list[DraftItemArgs] | None = Field(default=None, description="要下单的商品行")
    currency: str | None = Field(default=None, description="订单币种，ISO-4217 三位码，默认 CNY")


class GetOrderArgs(BaseModel):
    order_id: str | None = Field(default=None, description="订单号，例如 ord_xxx")


def make_order_tools(service: OrderService) -> list[FunctionTool]:
    """Build ``[create_order_draft, get_order]``; the order is fixed."""

    async def create_order_draft(
        items: list[DraftItemArgs] | None = None,
        currency: str | None = None,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        identity = _identity(_agent_state)
        if identity is None:
            return _result(
                {
                    "ok": False,
                    "kind": "order_draft",
                    "error": "missing_identity",
                    "message": "缺少买家身份，无法创建草稿",
                }
            )
        buyer_id, shopping_session_id = identity
        # ``_identity`` only returns a pair when the state exists, but mypy
        # cannot see through it.
        assert _agent_state is not None

        # The framework advertises ``input_schema`` but does not validate it, so
        # every mistyped argument reaches this body. A ``ValidationError`` (or a
        # non-iterable ``items``) escaping here would become an ERROR tool
        # result, which ``run_turn`` turns into a whole-turn 502.
        try:
            parsed = [
                item if isinstance(item, DraftItemArgs) else DraftItemArgs.model_validate(item)
                for item in (items or [])
            ]
            request_items = [
                DraftRequestItem(
                    product_id=item.product_id or "",
                    sku_id=item.sku_id or "",
                    quantity=item.quantity if item.quantity is not None else 1,
                )
                for item in parsed
            ]
        except (ValidationError, TypeError) as exc:
            return _result(
                {
                    "ok": False,
                    "kind": "order_draft",
                    "error": "invalid_argument",
                    "message": f"订单参数格式不正确：{exc}",
                }
            )
        # ``currency`` is dict-keyed below; a non-string would raise TypeError
        # (unhashable) instead of the validation result the model can correct.
        if currency is not None and not isinstance(currency, str):
            return _result(
                {
                    "ok": False,
                    "kind": "order_draft",
                    "error": "invalid_argument",
                    "message": "currency 必须是 ISO-4217 三位币种代码",
                }
            )

        try:
            creation = await service.create_draft(
                buyer_id=buyer_id,
                shopping_session_id=shopping_session_id,
                items=request_items,
                currency=currency or "CNY",
                context_evidence=search_evidence_from_context(_agent_state.context),
                appendix_evidence=_appendix_evidence(_agent_state),
            )
        except OrderValidationError as exc:
            payload: dict = {
                "ok": False,
                "kind": "order_draft",
                "error": exc.code,
                "message": exc.message,
            }
            if exc.line is not None:
                payload["line"] = exc.line
            return _result(payload)
        except OrderStorageError:
            logger.exception("order storage failed while creating a draft")
            return _error_chunk("订单存储暂时不可用，请稍后再试")

        return _result(
            {
                "ok": True,
                "kind": "order_draft",
                "replayed": creation.replayed,
                "draft": _draft_payload(creation.draft),
                "next_step": "草稿已生成，需要买家在应用里确认后才会创建订单",
            }
        )

    async def get_order(
        order_id: str | None = None,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        identity = _identity(_agent_state)
        if identity is None:
            return _result(
                {
                    "ok": False,
                    "kind": "order",
                    "error": "missing_identity",
                    "message": "缺少买家身份，无法查询订单",
                }
            )
        # ``isinstance`` first: ``order_id=123`` would AttributeError on strip().
        if not isinstance(order_id, str) or not order_id.strip():
            return _result(
                {
                    "ok": False,
                    "kind": "order",
                    "error": "invalid_argument",
                    "message": "缺少 order_id",
                }
            )

        buyer_id, _ = identity
        try:
            order, request = await service.get_order(order_id=order_id, buyer_id=buyer_id)
        except OrderNotFound:
            return _result(
                {
                    "ok": False,
                    "kind": "order",
                    "error": "not_found",
                    "message": f"订单 {order_id} 不存在",
                }
            )
        except OrderStorageError:
            logger.exception("order storage failed while reading an order")
            return _error_chunk("订单存储暂时不可用，请稍后再试")

        return _result(
            {
                "ok": True,
                "kind": "order",
                "order": _order_payload(order),
                "cancellation": _cancellation_payload(request),
            }
        )

    return [
        FunctionTool(
            create_order_draft,
            name="create_order_draft",
            description=(
                "把用户要买的商品写成订单草稿。只填商品 ID、SKU ID 和数量；价格由系统根据"
                "检索证据确定，不要填价格。草稿不会下单，必须由买家在应用里确认。"
                "返回 ok=false 时按 error 修正参数后重试。"
            ),
            input_schema=CreateOrderDraftArgs,
            is_state_injected=True,
        ),
        FunctionTool(
            get_order,
            name="get_order",
            description="查询当前买家自己的订单状态，以及该订单是否有取消请求。",
            input_schema=GetOrderArgs,
            is_state_injected=True,
        ),
    ]


# -- helpers --------------------------------------------------------------


def _identity(state: AgentState | None) -> tuple[str, str] | None:
    if state is None:
        return None
    identity = state.middle_context.get(IDENTITY_STATE_KEY)
    if not isinstance(identity, dict):
        return None
    buyer_id = identity.get("buyer_id")
    shopping_session_id = identity.get("shopping_session_id")
    if not buyer_id or not shopping_session_id:
        return None
    return str(buyer_id), str(shopping_session_id)


def _appendix_evidence(state: AgentState) -> list[SearchEvidence]:
    """Recover SKU prices from the newest S7 compression appendix in context."""
    for message in reversed(state.context):
        text = message.get_text_content() or ""
        if PROTECTED_FACTS_HEADER not in text:
            continue
        evidence: list[SearchEvidence] = []
        for fact in facts_from_appendix(text):
            if fact.price_major is None:
                continue
            evidence.append(
                SearchEvidence(
                    product_id=None,
                    sku_id=fact.sku_id,
                    title=fact.title or "",
                    unit_price=Decimal(str(fact.price_major)),
                    currency=fact.currency or "CNY",
                )
            )
        return evidence
    return []


def _draft_payload(draft: OrderDraft) -> dict:
    return {
        "draft_id": draft.draft_id,
        "status": draft.status,
        "currency": draft.currency,
        "total": str(draft.total),
        "items": [
            {
                "product_id": item.product_id,
                "sku_id": item.sku_id,
                "title": item.title,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "currency": item.currency,
            }
            for item in draft.items
        ],
        "created_at": draft.created_at,
    }


def _order_payload(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "draft_id": order.draft_id,
        "status": order.status,
        "currency": order.currency,
        "total": str(order.total),
        "items": [
            {
                "product_id": item.product_id,
                "sku_id": item.sku_id,
                "title": item.title,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "currency": item.currency,
            }
            for item in order.items
        ],
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def _cancellation_payload(request: CancellationRequest | None) -> dict | None:
    if request is None:
        return None
    return {
        "request_id": request.request_id,
        "status": request.status,
        "reason": request.reason,
        "created_at": request.created_at,
        "decided_at": request.decided_at,
    }


def _result(payload: dict) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
        state=ToolResultState.SUCCESS,
    )


def _error_chunk(message: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
    )
