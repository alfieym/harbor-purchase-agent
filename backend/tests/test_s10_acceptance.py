"""S10 acceptance: the transaction loop end to end (spec §9), offline."""

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState

from harbor.features.chat.service import SessionStore, run_turn
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import DraftRequestItem, OrderService
from harbor.features.orders.store import SqliteOrderStore


class _FakeAgentWithCards:
    """Replies with cards only — it never mentions a draft."""

    def __init__(self) -> None:
        self.state = AgentState()

    async def reply(self, msg, structured_schema=None):
        self.state.context.append(msg)
        reply = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text="好的")],
            structured_output={"summary": "好的", "cards": []},
        )
        self.state.context.append(reply)
        return reply


def _service(tmp_path: Path) -> OrderService:
    path = tmp_path / "orders.sqlite3"
    return OrderService(SqliteOrderStore(path), SqliteEvidenceStore(path))


def _evidence() -> SearchEvidence:
    return SearchEvidence(
        product_id="P1",
        sku_id="P1-S1",
        title="猫饮水器",
        unit_price=Decimal("89.00"),
        currency="CNY",
    )


def test_intents_exposes_pending_draft_without_creating_order(tmp_path) -> None:
    # The fake agent's reply never mentions a draft; pending_draft must come
    # from the database, not from model text.
    service = _service(tmp_path)
    asyncio.run(
        service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
    )
    store = SessionStore(agent_factory=lambda state: _FakeAgentWithCards())

    response = asyncio.run(run_turn(store, "b1", "s1", "谢谢", order_service=service))

    assert response.pending_draft is not None
    assert response.pending_draft.total == Decimal("89.00")
    with sqlite3.connect(tmp_path / "orders.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_full_two_phase_flow(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        draft = (
            await service.create_draft(
                buyer_id="b1",
                shopping_session_id="s1",
                items=[DraftRequestItem("P1", "P1-S1", 2)],
                context_evidence=[_evidence()],
            )
        ).draft
        order, _ = await service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        request, _ = await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="acceptance-1"
        )
        before = await service.get_order(order_id=order.order_id, buyer_id="b1")
        cancelled, request2, _ = await service.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return draft, order, request, before, cancelled, request2

    draft, order, request, before, cancelled, request2 = asyncio.run(scenario())
    # ``draft`` is the snapshot taken before confirm, so read the stored row:
    # the state machine moved it, the object the caller held did not.
    with sqlite3.connect(tmp_path / "orders.sqlite3") as conn:
        stored = conn.execute(
            "SELECT status FROM order_drafts WHERE id = ?", (draft.draft_id,)
        ).fetchone()[0]
    assert stored == "confirmed"
    assert order.total == Decimal("178.00")
    assert before[0].status == "created" and before[1].status == "pending"
    assert cancelled.status == "cancelled" and request2.status == "confirmed"
