"""S10: the transaction service is assembled into chat, agent, and identity."""

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState
from qdrant_client import QdrantClient

import harbor.agents.factory as factory
from harbor.agents.factory import build_agent, build_runtime
from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.core.config import Settings
from harbor.features.chat.service import SessionStore, run_turn
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import DraftRequestItem, OrderService
from harbor.features.orders.store import SqliteOrderStore

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class _FakeAgent:
    def __init__(self) -> None:
        self.state = AgentState()


class _FakeAgentWithCards:
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


def _offline_runtime(monkeypatch, tmp_path, **settings_kwargs):
    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        memory_db_path=tmp_path / "memory.sqlite3",
        **settings_kwargs,
    )
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-123")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    return build_runtime(settings, data_path=FIXTURE)


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


def test_session_identity_carries_both_ids() -> None:
    seen = {}

    def factory(state):
        seen["state"] = state
        return _FakeAgent()

    store = SessionStore(agent_factory=factory)
    asyncio.run(store.get_or_create("b1", "s1"))
    assert seen["state"].middle_context[IDENTITY_STATE_KEY] == {
        "buyer_id": "b1",
        "shopping_session_id": "s1",
    }


def test_build_agent_registers_order_tools_only_with_a_service(monkeypatch, tmp_path) -> None:
    runtime = _offline_runtime(monkeypatch, tmp_path)
    service = _service(tmp_path)

    with_service = build_agent(runtime, order_service=service)
    without = build_agent(runtime)

    names = {tool.name for tool in with_service.toolkit.tool_groups[0].tools}
    assert {"create_order_draft", "get_order"} <= names
    assert "create_order_draft" not in {t.name for t in without.toolkit.tool_groups[0].tools}


def test_run_turn_returns_the_draft_from_the_database(tmp_path) -> None:
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

    response = asyncio.run(run_turn(store, "b1", "s1", "随便聊聊", order_service=service))

    assert response.pending_draft is not None
    assert response.pending_draft.draft_id.startswith("drf_")
