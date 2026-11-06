"""S10: model-facing order tools — draft creation and order lookup."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState

from harbor.agents.context_governance import PROTECTED_FACTS_HEADER
from harbor.agents.order_tools import make_order_tools
from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import DraftRequestItem, OrderService
from harbor.features.orders.store import SqliteOrderStore


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


def _state(buyer="b1", session="s1") -> AgentState:
    state = AgentState()
    state.middle_context[IDENTITY_STATE_KEY] = {
        "buyer_id": buyer,
        "shopping_session_id": session,
    }
    return state


def _state_with_search_hit(state: AgentState, price: float = 89.0) -> AgentState:
    state.context.append(
        Msg(
            name="purchase_agent",
            role="assistant",
            content=[
                ToolResultBlock(
                    id="call-1",
                    name="product_search",
                    output=[
                        TextBlock(
                            type="text",
                            text=json.dumps(
                                {
                                    "hits": [
                                        {
                                            "product_id": "P1",
                                            "title": "猫饮水器",
                                            "selected_sku_id": "P1-S1",
                                            "price_major": price,
                                            "currency": "CNY",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    )
    return state


def _state_with_appendix(state: AgentState) -> AgentState:
    state.context.append(
        Msg(
            name="system",
            role="system",
            content=[
                TextBlock(
                    type="text",
                    text=f"{PROTECTED_FACTS_HEADER}\n- P1-S1 | 猫饮水器 | ¥89.00 CNY",
                )
            ],
        )
    )
    return state


def test_draft_tool_creates_a_draft_but_no_order(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]

    chunk = asyncio.run(
        tool.call(
            _agent_state=_state_with_search_hit(_state()),
            items=[{"product_id": "P1", "sku_id": "P1-S1", "quantity": 2}],
            currency="CNY",
        )
    )
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["ok"] is True and payload["kind"] == "order_draft"
    assert payload["draft"]["total"] == "178.00"
    assert payload["draft"]["status"] == "pending"


def test_tool_call_with_no_arguments_is_a_validation_result(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(tool.call(_agent_state=_state()))
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["ok"] is False
    assert payload["error"] == "empty_items"


@pytest.mark.parametrize(
    "items, code",
    [
        ([{"product_id": "P1", "sku_id": "P1-S1", "quantity": 0}], "invalid_quantity"),
        (
            [
                {"product_id": "P1", "sku_id": "P1-S1"},
                {"product_id": "P1", "sku_id": "P1-S1"},
            ],
            "duplicate_sku",
        ),
        ([{"product_id": "P9", "sku_id": "P9-S1"}], "evidence_missing"),
    ],
)
def test_bad_items_are_ok_false_not_error(tmp_path, items, code) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(tool.call(_agent_state=_state_with_search_hit(_state()), items=items))
    assert chunk.state == ToolResultState.SUCCESS
    assert json.loads(chunk.content[0].text)["error"] == code


@pytest.mark.parametrize(
    "items",
    [
        [{"product_id": "P1", "sku_id": "P1-S1", "quantity": 2.5}],
        "foo",
        [1, 2],
        42,
    ],
)
def test_mistyped_arguments_are_ok_false_not_error(tmp_path, items) -> None:
    """A model fumbling a *type* must get a correction, not a 502.

    The framework does not validate ``input_schema``, so normalization runs in
    the body — and a ``ValidationError`` escaping the body would become an
    ERROR tool result, which ``run_turn`` escalates to a whole-turn 502.
    """
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(tool.call(_agent_state=_state_with_search_hit(_state()), items=items))

    assert chunk.state == ToolResultState.SUCCESS
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_argument"


def test_missing_identity_is_ok_false(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(
        tool.call(
            _agent_state=AgentState(),
            items=[{"product_id": "P1", "sku_id": "P1-S1"}],
        )
    )
    payload = json.loads(chunk.content[0].text)
    assert chunk.state == ToolResultState.SUCCESS
    assert payload["error"] == "missing_identity"


def test_tool_reads_appendix_evidence(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(
        tool.call(
            _agent_state=_state_with_appendix(_state()),
            items=[{"product_id": "P1", "sku_id": "P1-S1"}],
        )
    )
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is True
    assert payload["draft"]["total"] == "89.00"


def test_get_order_hides_other_buyers_order(tmp_path) -> None:
    service = _service(tmp_path)
    draft = asyncio.run(
        service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
    ).draft
    order, _ = asyncio.run(service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"))

    chunk = asyncio.run(
        make_order_tools(service)[1].call(_agent_state=_state(buyer="b2"), order_id=order.order_id)
    )
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "not_found"


def test_get_order_returns_latest_cancellation(tmp_path) -> None:
    service = _service(tmp_path)
    draft = asyncio.run(
        service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
    ).draft
    order, _ = asyncio.run(service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"))
    request, _ = asyncio.run(
        service.request_cancellation(order_id=order.order_id, buyer_id="b1", idempotency_key="k1")
    )

    chunk = asyncio.run(
        make_order_tools(service)[1].call(_agent_state=_state(), order_id=order.order_id)
    )
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is True
    assert payload["order"]["status"] == "created"
    assert payload["cancellation"]["request_id"] == request.request_id


def test_mistyped_order_id_is_ok_false_not_error(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[1]
    chunk = asyncio.run(tool.call(_agent_state=_state(), order_id=123))
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["error"] == "invalid_argument"


def test_mistyped_currency_is_ok_false_not_error(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(
        tool.call(
            _agent_state=_state_with_search_hit(_state()),
            items=[{"product_id": "P1", "sku_id": "P1-S1"}],
            currency=["CNY"],
        )
    )
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["error"] == "invalid_argument"


def test_missing_order_id_is_ok_false(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[1]
    chunk = asyncio.run(tool.call(_agent_state=_state()))
    payload = json.loads(chunk.content[0].text)
    assert chunk.state == ToolResultState.SUCCESS
    assert payload["error"] == "invalid_argument"
