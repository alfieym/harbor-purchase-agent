"""S5: SessionStore keyed by (buyer_id, shopping_session_id) + run_turn (offline).

No model, index or network: the agent is stubbed and the clock is injected, so
these tests pin the session lifecycle, the timeout/tool-failure mapping, and the
reply→cards parsing exactly.
"""

import asyncio
import json
import logging

import pytest
from agentscope.message import (
    Msg,
    TextBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.state import AgentState

from harbor.features.chat.repository import SessionRepository, SnapshotWriteError
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
        hits: list[dict] | None = None,
    ) -> None:
        self._output = output
        self._text = text
        self._tool_state = tool_state
        self._delay = delay
        self._hits = hits
        self.state = FakeState()
        self.replies = 0

    async def reply(self, msg, structured_schema=None) -> FakeMsg:
        self.replies += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._hits is not None:
            self.state.context.append(_search_message(self._hits, call_id=f"call-{self.replies}"))
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

    def __init__(
        self, output: dict | None = None, text: str = "", state: AgentState | None = None
    ) -> None:
        self.state = state if state is not None else AgentState()
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


def _search_message(hits: list[dict], *, call_id: str) -> Msg:
    return Msg(
        name="purchase_agent",
        role="assistant",
        content=[
            ToolResultBlock(
                id=call_id,
                name="product_search",
                output=[
                    TextBlock(type="text", text=json.dumps({"hits": hits}, ensure_ascii=False))
                ],
                state=ToolResultState.SUCCESS,
            )
        ],
    )


class GroundedAgent:
    """Stub that can append a tool result before returning its cards."""

    def __init__(
        self,
        output: dict | None = None,
        *,
        hits: list[dict] | None = None,
        history: list[Msg] | None = None,
    ) -> None:
        self.state = AgentState()
        self.state.context.extend(history or [])
        self._output = output
        self._hits = hits

    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context.append(msg)
        if self._hits is not None:
            self.state.context.append(_search_message(self._hits, call_id="call-this-turn"))
        result = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text="命中 1 条")],
            structured_output=self._output,
        )
        self.state.context.append(result)
        return result


_HIT = {"product_id": "X1", "title": "猫饮水器", "selected_sku_id": "X1-S1"}


def test_run_turn_drops_cards_without_this_turn_evidence() -> None:
    """A chit-chat turn must not reprint the previous turn's cards."""
    agent = GroundedAgent(_cards_dict())

    async def body():
        return await run_turn(
            SessionStore(agent_factory=lambda state=None: agent), "u1", "s1", "你好"
        )

    response = asyncio.run(body())

    assert response.cards == []
    assert response.final_text == "命中 1 条"


def test_run_turn_keeps_a_card_backed_by_this_turns_search() -> None:
    agent = GroundedAgent(_cards_dict(), hits=[_HIT])

    async def body():
        return await run_turn(
            SessionStore(agent_factory=lambda state=None: agent), "u1", "s1", "给猫买饮水器"
        )

    response = asyncio.run(body())

    assert [card.sku_id for card in response.cards] == ["X1-S1"]


def test_run_turn_drops_a_card_backed_only_by_an_earlier_turn() -> None:
    old = _search_message([_HIT], call_id="call-earlier-turn")
    agent = GroundedAgent(_cards_dict(), history=[old])

    async def body():
        return await run_turn(
            SessionStore(agent_factory=lambda state=None: agent), "u1", "s1", "你好"
        )

    response = asyncio.run(body())

    assert response.cards == []


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


def test_lru_evict_skips_inflight_session() -> None:
    async def body() -> None:
        store = SessionStore(agent_factory=lambda state=None: FakeAgent(), max_sessions=3)
        _, s_a = await store.get_or_create("u1", "a")
        await store.get_or_create("u1", "b")
        await store.get_or_create("u1", "c")
        await s_a.lock.acquire()  # 模拟 a 正在 reply 中（持锁）
        try:
            await store.get_or_create("u1", "d")  # 触发逐出，必须跳过锁住的 a
        finally:
            s_a.lock.release()
        assert ("u1", "a") in store._sessions
        assert len(store) == 3

    asyncio.run(body())


def test_run_turn_parses_structured_output_and_resumes_session() -> None:
    created: list[FakeAgent] = []

    def factory(state=None) -> FakeAgent:
        agent = FakeAgent(output=_cards_dict("命中 1 条"), hits=[_HIT])
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


def test_get_or_create_starts_a_fresh_state_when_no_snapshot(tmp_path) -> None:
    repo = SessionRepository(tmp_path)
    received: list[AgentState | None] = []

    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())

    store = SessionStore(agent_factory=factory, repository=repo)
    asyncio.run(store.get_or_create("u1", "s1"))
    assert received[0] is not None
    assert received[0].context == []


def test_session_store_injects_buyer_identity_into_state() -> None:
    received: list[AgentState | None] = []

    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())

    store = SessionStore(agent_factory=factory)
    asyncio.run(store.get_or_create("u7", "s1"))

    assert received[0] is not None
    assert received[0].middle_context["harbor.identity"] == {"buyer_id": "u7"}


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


def test_run_turn_retries_when_session_evicted_while_waiting() -> None:
    """A turn must not reuse a session evicted by a concurrent failed turn.

    request A fails and evicts the session while request B is already blocked on
    the session's lock. B must detect the eviction after acquiring the lock and
    reload a fresh session, instead of running on A's polluted ``state.context``.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    created: list[object] = []

    class GatedFailAgent:
        def __init__(self) -> None:
            self.state = FakeState()

        async def reply(self, msg, structured_schema=None) -> FakeMsg:
            entered.set()
            await release.wait()
            self.state.context.append(FakeMsg(tool_state=ToolResultState.ERROR))
            return FakeMsg(None, "err")

    def factory(state=None):
        agent = GatedFailAgent() if not created else FakeAgent(output=_cards_dict())
        created.append(agent)
        return agent

    store = SessionStore(agent_factory=factory)

    async def body() -> tuple[object, object]:
        t1 = asyncio.create_task(run_turn(store, "u1", "s1", "first"))
        await entered.wait()
        t2 = asyncio.create_task(run_turn(store, "u1", "s1", "second"))
        await asyncio.sleep(0)  # let t2 fetch the cached session and block on its lock
        release.set()
        return await asyncio.gather(t1, t2, return_exceptions=True)

    first, second = asyncio.run(body())
    assert isinstance(first, ToolFailureError)
    assert isinstance(second, CommerceIntentResponse)
    assert len(created) == 2  # the second turn reloaded a fresh session, not the evicted one


def test_persist_logs_snapshot_write_failure(tmp_path, caplog) -> None:
    """A failed snapshot write (disk full / permissions / serialization) must log
    its root cause server-side, not surface a 500 with no trace."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    store = SessionStore(
        agent_factory=lambda state=None: FakeAgent(),
        repository=SessionRepository(blocker),
    )
    agent = PersistingAgent(output=_cards_dict())
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    with caplog.at_level(logging.ERROR, logger="harbor.features.chat.service"):
        with pytest.raises(SnapshotWriteError):
            asyncio.run(store.persist(("u1", "s1"), agent, "hi", response))
    assert any("snapshot write failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# S6 acceptance: restart continuity, journal turn numbering, persistence
# under same-session concurrency.
# --------------------------------------------------------------------------


def _restart(tmp_path, seen: list[tuple[AgentState | None, int]]) -> SessionStore:
    """A fresh process: new SessionStore over the same on-disk repository.

    Captures the state *as the factory received it* (a live reference would keep
    growing as the new turn appends messages).
    """

    def factory(state: AgentState | None = None) -> PersistingAgent:
        seen.append((state, len(state.context) if state is not None else 0))
        return PersistingAgent(output=_cards_dict(), state=state)

    return SessionStore(agent_factory=factory, repository=SessionRepository(tmp_path))


def test_restart_restores_previous_turns_and_continues(tmp_path) -> None:
    """S6-1/S6-6: after a process restart the next turn sees the full history."""
    repo = SessionRepository(tmp_path)
    store1 = SessionStore(
        agent_factory=lambda state=None: PersistingAgent(output=_cards_dict()),
        repository=repo,
    )
    asyncio.run(run_turn(store1, "u1", "s1", "给猫买个饮水器"))

    seen: list[tuple[AgentState | None, int]] = []
    asyncio.run(run_turn(_restart(tmp_path, seen), "u1", "s1", "刚才第一个多少钱"))

    state, restored_count = seen[0]
    assert state is not None
    assert restored_count == 2  # 上一轮的 user + assistant 都回来了
    assert state.context[0].get_text_content() == "给猫买个饮水器"
    # S6-6: 上一轮推荐的具体商品仍在结构化输出里
    assert state.context[1].structured_output["cards"][0]["sku_id"] == "X1-S1"

    restored = repo.load(("u1", "s1"))
    assert restored is not None and len(restored.context) == 4


def test_journal_row_count_matches_turns_across_restart(tmp_path) -> None:
    """S6-2: exactly 2 journal rows per successful turn, turn numbers continue."""
    repo = SessionRepository(tmp_path)
    store1 = SessionStore(
        agent_factory=lambda state=None: PersistingAgent(output=_cards_dict()),
        repository=repo,
    )
    asyncio.run(run_turn(store1, "u1", "s1", "q1"))

    seen: list[tuple[AgentState | None, int]] = []
    store2 = _restart(tmp_path, seen)
    asyncio.run(run_turn(store2, "u1", "s1", "q2"))
    asyncio.run(run_turn(store2, "u1", "s1", "q3"))

    lines = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(lines) == 6  # 3 轮 × 2 行
    assert [line["turn"] for line in lines] == [1, 1, 2, 2, 3, 3]
    assert [line["role"] for line in lines] == ["user", "assistant"] * 3


def test_same_session_concurrent_turns_persist_both(tmp_path) -> None:
    """S6-4 (persistent version): the session lock keeps two concurrent turns
    from clobbering each other's snapshot."""
    repo = SessionRepository(tmp_path)
    store = SessionStore(
        agent_factory=lambda state=None: PersistingAgent(output=_cards_dict()),
        repository=repo,
    )

    async def body() -> None:
        await asyncio.gather(
            run_turn(store, "u1", "s1", "第一个问题"),
            run_turn(store, "u1", "s1", "第二个问题"),
        )

    asyncio.run(body())
    restored = repo.load(("u1", "s1"))
    assert restored is not None and len(restored.context) == 4
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(journal) == 4


class CompressingAgent:
    """A real ``AgentState`` whose reply compresses away the old context.

    S7 compresses in the middle of a turn, so ``context_start`` (captured
    before the reply) can point past the end of the shrunk list. Tool failures
    must still be attributed to this turn.
    """

    def __init__(self) -> None:
        self.state = AgentState()
        self.state.context = [UserMsg(name="user", content=f"old {i}") for i in range(10)]

    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context = [msg]
        self.state.context.append(
            Msg(
                name="purchase_agent",
                role="assistant",
                content=[
                    ToolResultBlock(
                        id="call-1",
                        name="product_search",
                        output="boom",
                        state=ToolResultState.ERROR,
                    )
                ],
            )
        )
        return Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(text="我猜的")],
            structured_output={"summary": "x", "cards": []},
        )


def test_run_turn_detects_tool_failure_after_mid_turn_compression() -> None:
    store = SessionStore(agent_factory=lambda state=None: CompressingAgent())

    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "给猫买个饮水器"))
