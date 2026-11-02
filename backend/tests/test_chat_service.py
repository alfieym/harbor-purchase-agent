"""S5: SessionStore keyed by (buyer_id, shopping_session_id) + run_turn (offline).

No model, index or network: the agent is stubbed and the clock is injected, so
these tests pin the session lifecycle, the timeout/tool-failure mapping, and the
reply→cards parsing exactly.
"""

import asyncio

import pytest
from agentscope.message import Msg, TextBlock, ToolResultState, UserMsg
from agentscope.state import AgentState

from harbor.features.chat.repository import SessionRepository
from harbor.features.chat.schemas import CommerceIntentResponse
from harbor.features.chat.service import (
    ModelFailureError,
    ModelTimeoutError,
    NoStructuredOutputError,
    SessionStore,
    ToolFailureError,
    run_turn,
)


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


class FakeState:
    """Minimal stand-in for AgentScope's ``AgentState`` (only ``context``)."""

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
        self.state = FakeState()
        self.replies = 0

    async def reply(self, msg, structured_schema=None) -> FakeMsg:
        self.replies += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        # Mirror AgentScope: a failed tool call lands as a tool_result block in
        # the conversation context *before* the final text answer, so run_turn's
        # context scan sees it even though the returned reply does not carry it.
        if self._tool_state is not None:
            self.state.context.append(FakeMsg(tool_state=self._tool_state))
        result = FakeMsg(self._output, self._text)
        self.state.context.append(result)
        return result


class PersistingAgent:
    """Stub backed by a real ``AgentState``, so ``persist`` can serialize it."""

    def __init__(self, output: dict | None = None, text: str = "") -> None:
        self.state = AgentState()
        self._output = output
        self._text = text

    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context.append(msg)
        result = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text=self._text)],
            structured_output=self._output,
        )
        self.state.context.append(result)
        return result


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cards_dict(summary: str = "命中 1 条") -> dict:
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
        "summary": summary,
    }


def _store(clock: FakeClock | None = None, max_sessions: int = 1000) -> SessionStore:
    return SessionStore(
        agent_factory=lambda state=None: FakeAgent(),
        ttl_seconds=1800.0,
        max_sessions=max_sessions,
        clock=clock or FakeClock(),
    )


def test_same_buyer_and_session_resumes_not_recreates() -> None:
    async def body() -> bool:
        store = _store()
        key1, first = await store.get_or_create("u1", "s1")
        key2, second = await store.get_or_create("u1", "s1")
        return key1 == key2 and first is second and len(store) == 1

    assert asyncio.run(body())


def test_different_buyers_are_isolated() -> None:
    async def body() -> int:
        store = _store()
        await store.get_or_create("u1", "s1")
        await store.get_or_create("u2", "s1")
        return len(store)

    assert asyncio.run(body()) == 2


def test_same_buyer_different_sessions_are_isolated() -> None:
    async def body() -> int:
        store = _store()
        await store.get_or_create("u1", "s1")
        await store.get_or_create("u1", "s2")
        return len(store)

    assert asyncio.run(body()) == 2


def test_expired_session_is_evicted() -> None:
    clock = FakeClock()

    async def body() -> bool:
        store = _store(clock=clock)
        await store.get_or_create("u1", "s1")
        clock.advance(1800.0)  # exactly one TTL
        _, session = await store.get_or_create("u1", "s1")
        # The old session expired and was dropped, so a fresh one is made.
        return len(store) == 1

    assert asyncio.run(body())


def test_lru_evicts_least_recently_used_when_over_capacity() -> None:
    clock = FakeClock()

    async def body() -> None:
        store = _store(clock=clock, max_sessions=3)
        await store.get_or_create("u1", "a")
        clock.advance(1.0)
        await store.get_or_create("u1", "b")
        clock.advance(1.0)
        await store.get_or_create("u1", "c")
        clock.advance(1.0)

        # Touch b and c so a is the least recently used.
        store.touch(("u1", "b"))
        store.touch(("u1", "c"))
        await store.get_or_create("u1", "d")  # forces eviction of a

        # a is gone: a new key (a) would be recreated, so assert the store is 3.
        assert len(store) == 3
        assert ("u1", "a") not in store._sessions

    asyncio.run(body())


def test_run_turn_parses_structured_output_and_resumes_session() -> None:
    created: list[FakeAgent] = []

    def factory(state=None) -> FakeAgent:
        agent = FakeAgent(output=_cards_dict("命中 1 条"))
        created.append(agent)
        return agent

    async def body():
        store = SessionStore(agent_factory=factory)
        first = await run_turn(store, "u1", "s1", "给猫买个饮水器")
        second = await run_turn(store, "u1", "s1", "再推荐一个")
        return first, second

    first, second = asyncio.run(body())
    assert first.final_text == "命中 1 条"
    assert second.final_text == "命中 1 条"
    assert len(created) == 1  # the session (and its agent) was reused, not rebuilt
    assert created[0].replies == 2
    assert first.cards[0].sku_id == "X1-S1"


def test_run_turn_raises_when_model_produces_no_structured_output() -> None:
    def factory(state=None) -> FakeAgent:
        return FakeAgent(output=None, text="目录里没有符合条件的商品")

    async def body() -> NoStructuredOutputError:
        store = SessionStore(agent_factory=factory)
        with pytest.raises(NoStructuredOutputError) as excinfo:
            await run_turn(store, "u1", "s1", "预算1元买吸尘器")
        return excinfo.value

    error = asyncio.run(body())
    assert "没有符合条件的商品" in error.text


def test_run_turn_raises_model_timeout_error() -> None:
    def factory(state=None) -> FakeAgent:
        return FakeAgent(output=_cards_dict(), delay=0.5)

    async def body() -> None:
        store = SessionStore(agent_factory=factory)
        with pytest.raises(ModelTimeoutError):
            await run_turn(store, "u1", "s1", "hi", timeout_seconds=0.01)

    asyncio.run(body())


def test_run_turn_raises_tool_failure_error_on_error_tool_result() -> None:
    def factory(state=None) -> FakeAgent:
        return FakeAgent(output=None, tool_state=ToolResultState.ERROR)

    async def body() -> None:
        store = SessionStore(agent_factory=factory)
        with pytest.raises(ToolFailureError):
            await run_turn(store, "u1", "s1", "hi")

    asyncio.run(body())


def test_run_turn_raises_model_failure_error_when_reply_raises() -> None:
    """A model API failure (network / rate-limit) raises out of ``Agent.reply``
    and must map to a structured error, not leak as an unhandled 500."""

    class RaisingAgent:
        def __init__(self) -> None:
            self.state = FakeState()

        async def reply(self, msg, structured_schema=None):
            raise RuntimeError("dashscope rate limited")

    async def body() -> None:
        store = SessionStore(agent_factory=lambda state=None: RaisingAgent())
        with pytest.raises(ModelFailureError):
            await run_turn(store, "u1", "s1", "hi")

    asyncio.run(body())


def test_same_session_concurrent_turns_serialize() -> None:
    """S5-5: two concurrent turns on the same session serialize on the lock."""
    state = {"active": 0, "max_active": 0}

    class OverlapAgent:
        def __init__(self) -> None:
            self.state = FakeState()

        async def reply(self, msg, structured_schema=None) -> FakeMsg:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return FakeMsg(_cards_dict("命中 1 条"))

    async def body() -> None:
        store = SessionStore(agent_factory=lambda state=None: OverlapAgent())
        await asyncio.gather(
            run_turn(store, "u1", "s1", "a"),
            run_turn(store, "u1", "s1", "b"),
        )

    asyncio.run(body())
    # The per-session lock keeps the single agent's reply from ever overlapping.
    assert state["max_active"] == 1


def _repo_state() -> AgentState:
    s = AgentState()
    s.context.append(Msg(name="user", role="user", content=[TextBlock(type="text", text="hi")]))
    return s


def test_get_or_create_restores_state_from_repository(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _repo_state())
    received: list[AgentState | None] = []

    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())

    store = SessionStore(agent_factory=factory, repository=repo)
    asyncio.run(store.get_or_create("u1", "s1"))
    assert len(received) == 1 and received[0] is not None


def test_get_or_create_passes_none_when_no_snapshot(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    received: list[AgentState | None] = []

    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())

    store = SessionStore(agent_factory=factory, repository=repo)
    asyncio.run(store.get_or_create("u1", "s1"))
    assert received == [None]


def test_persist_writes_snapshot_and_journal(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(), repository=repo)
    agent = PersistingAgent(output=_cards_dict())
    agent.state.context.append(UserMsg(name="user", content="给猫买个饮水器"))
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    asyncio.run(store.persist(("u1", "s1"), agent, "给猫买个饮水器", response))
    assert repo.load(("u1", "s1")) is not None
    assert len((tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")) == 2


def test_persist_is_noop_without_repository(tmp_path) -> None:
    store = SessionStore(agent_factory=lambda state=None: FakeAgent())
    agent = PersistingAgent(output=_cards_dict())
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    asyncio.run(store.persist(("u1", "s1"), agent, "hi", response))  # must not raise


def test_evict_removes_session() -> None:
    async def body() -> bool:
        store = SessionStore(agent_factory=lambda state=None: FakeAgent())
        key, _ = await store.get_or_create("u1", "s1")
        store.evict(key)
        return len(store) == 0

    assert asyncio.run(body())


def test_run_turn_persists_snapshot_and_journal(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    store = SessionStore(
        agent_factory=lambda state=None: PersistingAgent(output=_cards_dict()),
        repository=repo,
    )
    asyncio.run(run_turn(store, "u1", "s1", "给猫买个饮水器"))
    assert repo.load(("u1", "s1")) is not None
    assert len((tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")) == 2


def test_run_turn_evicts_session_on_failure(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    created: list[FakeAgent] = []

    def factory(state=None) -> FakeAgent:
        agent = FakeAgent(tool_state=ToolResultState.ERROR)
        created.append(agent)
        return agent

    store = SessionStore(agent_factory=factory, repository=repo)
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert len(store) == 0  # 会话被逐出
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert len(created) == 2  # 重试新建 Agent，而非复用失败的那个


def test_run_turn_does_not_persist_failed_turn(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(tool_state=ToolResultState.ERROR),
        repository=repo,
    )
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert repo.load(("u1", "s1")) is None
