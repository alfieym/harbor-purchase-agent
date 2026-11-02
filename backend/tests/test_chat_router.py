"""S5: POST /commerce/intents routing + error envelope (offline, stub agent)."""

import asyncio

from agentscope.message import ToolResultState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.core.errors import install_error_handlers
from harbor.features.chat.repository import SnapshotWriteError
from harbor.features.chat.router import router
from harbor.features.chat.service import SessionStore


class _ToolResult:
    def __init__(self, state: ToolResultState) -> None:
        self.state = state


class FakeMsg:
    def __init__(
        self,
        structured_output: dict | None = None,
        text: str = "",
        tool_state: ToolResultState | None = None,
    ) -> None:
        self.structured_output = structured_output
        self._text = text
        self._tool_state = tool_state

    def get_text_content(self) -> str:
        return self._text

    def get_content_blocks(self, kind: str) -> list[_ToolResult]:
        if kind == "tool_result" and self._tool_state is not None:
            return [_ToolResult(self._tool_state)]
        return []


class _FakeState:
    def __init__(self) -> None:
        self.context: list[FakeMsg] = []


class FakeAgent:
    def __init__(
        self,
        output: dict | None = None,
        text: str = "",
        tool_state: ToolResultState | None = None,
        delay: float = 0.0,
    ) -> None:
        self._output = output
        self._text = text
        self._tool_state = tool_state
        self._delay = delay
        self.state = _FakeState()

    async def reply(self, msg, structured_schema=None) -> FakeMsg:
        if self._delay:
            await asyncio.sleep(self._delay)
        # Mirror AgentScope: a tool failure lands in the conversation context
        # before the final answer, where run_turn detects it (S5-2).
        if self._tool_state is not None:
            self.state.context.append(FakeMsg(tool_state=self._tool_state))
        result = FakeMsg(self._output, self._text)
        self.state.context.append(result)
        return result


def _cards_dict() -> dict:
    return {
        "cards": [
            {
                "title": "猫饮水器",
                "brand": "PetGo",
                "category": "母婴宠物",
                "price_major": 200.0,
                "currency": "CNY",
                "weight_kg": 1.0,
                "material_tags": [],
                "highlights": [],
                "sku_id": "X1-S1",
                "score": None,
            }
        ],
        "summary": "命中 1 条",
    }


def _client(store: SessionStore | None, timeout_seconds: float = 120.0) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/commerce")
    app.state.chat_store = store
    app.state.settings = Settings(_env_file=None, turn_timeout_seconds=timeout_seconds)
    return TestClient(app)


class _FailingRepo:
    def load(self, key):
        return None

    def save(self, key, state):
        raise SnapshotWriteError()

    def append_journal(self, *args, **kwargs):
        pass


def test_intents_returns_final_text_and_cards() -> None:
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(output=_cards_dict()))
    with _client(store) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "给猫买个饮水器"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["final_text"] == "命中 1 条"
    assert body["cards"][0]["sku_id"] == "X1-S1"


def test_intents_resumes_same_buyer_session() -> None:
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(output=_cards_dict()))
    with _client(store) as client:
        client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "again"},
        )

    assert resp.status_code == 200


def test_intents_no_structured_output_returns_502() -> None:
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(output=None, text="目录里没有命中")
    )
    with _client(store) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "预算1元买吸尘器"},
        )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "http_502"


def test_intents_tool_failure_returns_502() -> None:
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(tool_state=ToolResultState.ERROR)
    )
    with _client(store) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "http_502"


def test_intents_model_failure_returns_502() -> None:
    class RaisingAgent:
        def __init__(self) -> None:
            self.state = _FakeState()

        async def reply(self, msg, structured_schema=None):
            raise RuntimeError("provider rate limited")

    store = SessionStore(agent_factory=lambda state=None: RaisingAgent())
    with _client(store) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "http_502"


def test_intents_model_timeout_returns_504() -> None:
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(output=_cards_dict(), delay=0.5)
    )
    with _client(store, timeout_seconds=0.01) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "http_504"


def test_intents_not_configured_returns_503() -> None:
    with _client(None) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "http_503"


def test_intents_snapshot_failure_returns_500() -> None:
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(output=_cards_dict()),
        repository=_FailingRepo(),
    )
    with _client(store) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "http_500"
