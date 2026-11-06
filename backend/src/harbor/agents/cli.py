"""Interactive REPL: the model calls ``product_search`` and returns product cards (S4).

Usage:
    uv run --directory backend python -m harbor.agents

Requires a model API key (``OPENAI_API_KEY`` / ``HARBOR_OPENAI_API_KEY``). Building the
index needs a ``/embeddings``-capable endpoint (query-time embedding failure still
degrades to keyword_2gram, but indexing itself needs the embedder once).

The index is persisted under ``.cache/catalog-index/`` so restarts skip embedding.
A fingerprint of the catalog file + embedding config + index mode invalidates it on
change. Qdrant local mode locks that directory, so run one CLI at a time.

All assembly lives in ``harbor.agents.factory`` (S5), shared with the HTTP chat
endpoint. This module keeps only the interactive REPL loop.
"""

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from agentscope.agent import Agent
from agentscope.message import UserMsg

from harbor.agents.cards import ProductCardList
from harbor.agents.factory import build_agent, build_runtime
from harbor.agents.grounding import ground_cards, tool_results_since
from harbor.core.config import get_settings
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import Order, OrderDraft
from harbor.features.orders.service import (
    OrderConflict,
    OrderNotFound,
    OrderService,
    OrderServiceError,
)
from harbor.features.orders.store import SqliteOrderStore

CLI_BUYER_ID = "cli"
CLI_SESSION_ID = "cli"

USAGE = """订单命令：
  /draft                     查看待确认草稿
  /confirm <draft_id>        确认草稿，创建订单
  /cancel-draft <draft_id>   取消草稿
  /order <order_id>          查看订单
  /cancel-order <order_id> [原因]   申请取消订单
  /confirm-cancel <order_id>        确认取消订单
  /withdraw-cancel <order_id>       撤回取消申请"""


def _print_cards(cards: ProductCardList) -> None:
    print(f"\n助手：{cards.summary}")
    for index, card in enumerate(cards.cards, start=1):
        tags = " / ".join(card.material_tags) if card.material_tags else "—"
        print(
            f"  {index}. {card.title}  [{card.brand}]  "
            f"{card.price_major:g} {card.currency} · {card.weight_kg:g}kg · "
            f"{card.category} · {tags}"
        )


def _draft_summary(draft: OrderDraft) -> str:
    lines = [f"草稿 {draft.draft_id}（{draft.status}）：{draft.total} {draft.currency}"]
    for item in draft.items:
        lines.append(f"  - {item.title} × {item.quantity} @ {item.unit_price} {item.currency}")
    return "\n".join(lines)


def _order_summary(order: Order) -> str:
    lines = [f"订单 {order.order_id}（{order.status}）：{order.total} {order.currency}"]
    for item in order.items:
        lines.append(f"  - {item.title} × {item.quantity} @ {item.unit_price} {item.currency}")
    return "\n".join(lines)


# -- slash commands -------------------------------------------------------
#
# The human gate in the REPL. The model never sees these strings: a command
# line is answered here and never reaches ``agent.reply`` (S10).

CommandHandler = Callable[..., Awaitable[str]]


async def _cmd_draft(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    draft = await service.get_pending_draft(
        buyer_id=buyer_id, shopping_session_id=shopping_session_id
    )
    if draft is None:
        return "没有待确认的草稿。"
    return f"{_draft_summary(draft)}\n输入 /confirm {draft.draft_id} 下单。"


async def _cmd_confirm(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    order, idempotent = await service.confirm_draft(draft_id=args[0], buyer_id=buyer_id)
    suffix = "（重复确认，返回同一订单）" if idempotent else ""
    return f"{_order_summary(order)}{suffix}"


async def _cmd_cancel_draft(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    draft, _ = await service.cancel_draft(draft_id=args[0], buyer_id=buyer_id)
    return f"草稿 {draft.draft_id} 已取消。"


async def _cmd_order(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    order, request = await service.get_order(order_id=args[0], buyer_id=buyer_id)
    text = _order_summary(order)
    if request is not None:
        text += f"\n取消申请 {request.request_id}：{request.status}"
    return text


async def _cmd_cancel_order(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    reason = " ".join(args[1:]) or None
    request, _ = await service.request_cancellation(
        order_id=args[0],
        buyer_id=buyer_id,
        idempotency_key=uuid.uuid4().hex,
        reason=reason,
    )
    return (
        f"已提交取消申请 {request.request_id}（{request.status}）。"
        f"\n输入 /confirm-cancel {args[0]} 确认取消。"
    )


async def _cmd_confirm_cancel(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    order, _, idempotent = await service.confirm_cancellation(order_id=args[0], buyer_id=buyer_id)
    suffix = "（重复确认，订单已是取消状态）" if idempotent else ""
    return f"{_order_summary(order)}{suffix}"


async def _cmd_withdraw_cancel(
    service: OrderService, args: list[str], *, buyer_id: str, shopping_session_id: str
) -> str:
    request, _ = await service.withdraw_cancellation(order_id=args[0], buyer_id=buyer_id)
    return f"已撤回取消申请 {request.request_id}，订单保持原状态。"


# ``(handler, min_args, max_args)``; arity is checked before the service, so a
# bare ``/confirm`` gets usage rather than a misleading "not configured".
_COMMANDS: dict[str, tuple[CommandHandler, int, int]] = {
    "/draft": (_cmd_draft, 0, 0),
    "/confirm": (_cmd_confirm, 1, 1),
    "/cancel-draft": (_cmd_cancel_draft, 1, 1),
    "/order": (_cmd_order, 1, 1),
    "/cancel-order": (_cmd_cancel_order, 1, 100),
    "/confirm-cancel": (_cmd_confirm_cancel, 1, 1),
    "/withdraw-cancel": (_cmd_withdraw_cancel, 1, 1),
}


async def handle_command(
    text: str,
    service: OrderService | None,
    *,
    buyer_id: str,
    shopping_session_id: str,
) -> str | None:
    """Answer a slash command, or return ``None`` to hand the text to the model."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command, args = parts[0], parts[1:]

    entry = _COMMANDS.get(command)
    if entry is None:
        return f"未知命令 {command}。\n{USAGE}"
    handler, min_args, max_args = entry
    if not min_args <= len(args) <= max_args:
        return f"用法：{command} {'<参数>' if min_args else '（无参数）'}\n{USAGE}"
    if service is None:
        return "订单服务未配置。"
    try:
        return await handler(
            service,
            args,
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
        )
    except OrderNotFound as exc:
        return f"未找到：{exc}"
    except OrderConflict as exc:
        status = exc.current_status or "未知"
        return f"操作冲突，当前状态：{status}。{exc}"
    except OrderServiceError as exc:
        return f"订单服务出错：{exc}"


async def run_repl(
    agent: Agent,
    service: OrderService | None = None,
    *,
    buyer_id: str = CLI_BUYER_ID,
    shopping_session_id: str = CLI_SESSION_ID,
) -> None:
    print("Harbor 采购助手（输入 /quit 退出）")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text or text in {"/quit", "/exit"}:
            break
        # S10: a slash command is answered locally and never reaches the model.
        command_output = await handle_command(
            text,
            service,
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
        )
        if command_output is not None:
            print(command_output)
            continue
        # Snapshot the tool results *before* the turn, so cards copied from an
        # earlier answer cannot be presented as this turn's finds.
        known_results = {
            block.id
            for message in agent.state.context
            for block in message.get_content_blocks("tool_result")
            if getattr(block, "id", None) is not None
        }
        try:
            reply = await agent.reply(
                UserMsg(name="user", content=text), structured_schema=ProductCardList
            )
        except Exception as exc:  # noqa: BLE001 — surface model errors, keep the loop alive
            print(f"\n出错了：{exc}")
            continue
        structured = reply.structured_output
        if structured is None:
            print(f"\n助手（文本）：{reply.get_text_content()}")
        else:
            cards = ProductCardList.model_validate(structured)
            grounded = ground_cards(
                cards.cards, tool_results_since(agent.state.context, known_results)
            )
            _print_cards(cards.model_copy(update={"cards": grounded}))
        await _print_pending_draft(service, buyer_id, shopping_session_id)


async def _print_pending_draft(
    service: OrderService | None, buyer_id: str, shopping_session_id: str
) -> None:
    """After a model turn, show the draft the model created, if any (S10)."""
    if service is None:
        return
    try:
        draft = await service.get_pending_draft(
            buyer_id=buyer_id, shopping_session_id=shopping_session_id
        )
    except OrderServiceError as exc:
        print(f"\n读取待确认草稿失败：{exc}")
        return
    if draft is not None:
        print(f"\n{_draft_summary(draft)}\n输入 /confirm {draft.draft_id} 下单。")


def main(data_path: Path | None = None) -> None:
    settings = get_settings()
    if not settings.llm_configured:
        print(
            "未配置模型 API key。请设置 OPENAI_API_KEY（或 HARBOR_OPENAI_API_KEY）。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # S10: the REPL is the buyer, so it owns the application-layer actions the
    # model cannot take. Orders and evidence share the configured SQLite file.
    service = OrderService(
        SqliteOrderStore(settings.orders_db_path),
        SqliteEvidenceStore(
            settings.orders_db_path, retention_days=settings.evidence_retention_days
        ),
    )
    runtime = build_runtime(settings, data_path=data_path)
    # The CLI has one implicit buyer, so long-term memory still works there.
    agent = build_agent(
        runtime,
        buyer_id=CLI_BUYER_ID,
        shopping_session_id=CLI_SESSION_ID,
        order_service=service,
    )
    asyncio.run(run_repl(agent, service, buyer_id=CLI_BUYER_ID, shopping_session_id=CLI_SESSION_ID))


if __name__ == "__main__":
    main()
