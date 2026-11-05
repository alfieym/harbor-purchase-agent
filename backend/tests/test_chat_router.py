"""S5: POST /commerce/intents routing + error envelope (offline, stub agent)."""

import asyncio
import json

from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.core.errors import install_error_handlers
from harbor.features.chat.repository import SessionRepository, SnapshotWriteError
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
        hits: list[dict] | None = None,
    ) -> None:
        self._output = output
        self._text = text
        self._tool_state = tool_state
        self._delay = delay
        self._hits = hits
        self.state = _FakeState()

    async def reply(self, msg, structured_schema=None) -> FakeMsg:
        if self._delay:
            await asyncio.sleep(self._delay)
        # Mirror AgentScope: tool results land in the conversation context
        # before the final answer, where run_turn reads them.
        if self._hits is not None:
            self.state.context.append(_search_message(self._hits))
        if self._tool_state is not None:
            self.state.context.append(FakeMsg(tool_state=self._tool_state))
        result = FakeMsg(self._output, self._text)
        self.state.context.append(result)
        return result


_HIT = {"product_id": "X1", "title": "猫饮水器", "selected_sku_id": "X1-S1"}


def _search_message(hits: list[dict]) -> Msg:
    return Msg(
        name="purchase_agent",
        role="assistant",
        content=[
            ToolResultBlock(
                id="call-1",
                name="product_search",
                output=[
                    TextBlock(type="text", text=json.dumps({"hits": hits}, ensure_ascii=False))
                ],
                state=ToolResultState.SUCCESS,
            )
        ],
    )


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


def _client(
    store: SessionStore | None,
    timeout_seconds: float = 120.0,
    session_repository=None,
) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/commerce")
    app.state.chat_store = store
    app.state.session_repository = session_repository
    # Turn-only config: dispatch is off, so no batch budget has to fit.
    app.state.settings = Settings(
        _env_file=None, turn_timeout_seconds=timeout_seconds, dispatch_enabled=False
    )
    return TestClient(app)


class _FailingRepo:
    def load(self, key):
        return None

    def save(self, key, state):
        raise SnapshotWriteError()

    def append_journal(self, *args, **kwargs):
        pass


def test_intents_returns_final_text_and_cards() -> None:
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(output=_cards_dict(), hits=[_HIT])
    )
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


def test_intents_corrupt_snapshot_returns_500(tmp_path) -> None:
    """S6-3: a hand-broken snapshot must surface as an explicit error, not a
    silent fresh session."""
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(output=_cards_dict()),
        repository=repo,
    )
    with _client(store, session_repository=repo) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"},
        )

    assert resp.status_code == 500
    assert resp.json()["error"]["message"] == "会话快照损坏"
    assert path.exists()  # 坏文件保留，供人工排查


def _envelope(buyer: str, session: str, at: str, turns: int) -> dict:
    return {
        "schema_version": 1,
        "buyer_id": buyer,
        "shopping_session_id": session,
        "last_active_at": at,
        "turn_count": turns,
        "state": {},
    }


def _write_snapshot(repo: SessionRepository, key, doc: dict) -> None:
    path = repo.snapshot_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_sessions_lists_buyer_sessions_newest_first(tmp_path) -> None:
    """S6-5: GET /commerce/sessions?buyer_id= lists only that buyer, desc."""
    repo = SessionRepository(tmp_path)
    _write_snapshot(repo, ("u1", "old"), _envelope("u1", "old", "2026-11-01T10:00:00+00:00", 1))
    _write_snapshot(repo, ("u1", "new"), _envelope("u1", "new", "2026-11-03T10:00:00+00:00", 3))
    _write_snapshot(repo, ("u2", "other"), _envelope("u2", "other", "2026-11-04T10:00:00+00:00", 9))

    with _client(None, session_repository=repo) as client:
        resp = client.get("/commerce/sessions", params={"buyer_id": "u1"})

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert [s["shopping_session_id"] for s in sessions] == ["new", "old"]
    assert sessions[0]["turn_count"] == 3


def test_sessions_requires_buyer_id(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    with _client(None, session_repository=repo) as client:
        resp = client.get("/commerce/sessions")
    assert resp.status_code == 422


def test_sessions_unconfigured_returns_503() -> None:
    with _client(None, session_repository=None) as client:
        resp = client.get("/commerce/sessions", params={"buyer_id": "u1"})
    assert resp.status_code == 503
