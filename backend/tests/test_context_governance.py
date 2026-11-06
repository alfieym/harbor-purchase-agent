"""S7: context governance — archive pointers, fact validation, hard gate (offline).

These tests pin the three promises of S7 without any model or network call:

1. compaction never destroys the old messages (they land in an inspectable archive);
2. a summary that invents a product (or misquotes its price) is rolled back;
3. the token ceiling is a gate — a request that cannot fit never reaches the model.
"""

import asyncio
import copy
import json
from pathlib import Path

from agentscope.agent import Agent, ContextConfig
from agentscope.message import (
    AssistantMsg,
    Msg,
    SystemMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import StructuredResponse
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.agents.context_governance import (
    MAX_PREFERENCES,
    PROTECTED_FACTS_HEADER,
    ContextCapacityError,
    ContextGovernanceMiddleware,
    ProductFact,
    SessionArchiveOffloader,
    append_protected_facts,
    facts_from_appendix,
    preference_facts,
    product_facts,
    recorded_turn_count,
    validate_summary,
)
from harbor.agents.preferences import PreferenceMemory
from harbor.core.config import Settings
from harbor.core.errors import install_error_handlers
from harbor.features.chat.router import router
from harbor.features.chat.service import SessionStore, run_turn
from harbor.features.memory.sqlite_store import SqliteMemoryStore

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cards_message(*, sku: str = "P3472-S1", price: float = 567.0, call_id: str = "call-1") -> Msg:
    return AssistantMsg(
        name="purchase_agent",
        content=[
            ToolCallBlock(
                id=call_id,
                name="GenerateStructuredOutput",
                input=json.dumps(
                    {
                        "summary": "推荐 1 件",
                        "cards": [
                            {
                                "title": "猫用自动饮水器",
                                "brand": "PetGo",
                                "category": "母婴宠物",
                                "price_major": price,
                                "currency": "CNY",
                                "weight_kg": 1.0,
                                "material_tags": [],
                                "highlights": [],
                                "sku_id": sku,
                                "score": None,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        ],
    )


def _tool_result_message(*, call_id: str = "call-1", sku: str = "P3472-S1") -> Msg:
    return Msg(
        name="purchase_agent",
        role="assistant",
        content=[
            ToolResultBlock(
                id=call_id,
                name="GenerateStructuredOutput",
                output=json.dumps({"cards": [{"sku_id": sku, "price_major": 567.0}]}),
                state=ToolResultState.SUCCESS,
            )
        ],
    )


def _conversation() -> list[Msg]:
    """Three turns: u1/a1, u2/a2 (+cards), u3/a3 (+tool result)."""
    return [
        UserMsg(name="user", content="给猫买个饮水器"),
        AssistantMsg(name="purchase_agent", content=[TextBlock(text="好的，我找找")]),
        UserMsg(name="user", content="预算 600"),
        _cards_message(),
        UserMsg(name="user", content="第一个多少钱"),
        _tool_result_message(),
    ]


class FakeModel:
    context_size = 1000

    def __init__(self, tokens: int | None = None) -> None:
        # ``None`` means "estimate like the real model" (bytes / 4), which the
        # boundary-reserve test needs; an int pins the gate tests.
        self.tokens = tokens

    async def count_tokens(self, messages, tools=None) -> int:
        if self.tokens is not None:
            return self.tokens
        size = sum(len(json.dumps(m.model_dump(mode="json"), ensure_ascii=False)) for m in messages)
        return size // 4 + 1


class FakeAgent:
    """Only the surface the middleware touches."""

    def __init__(
        self, token_count: int | None = None, after_compress_tokens: int | None = None
    ) -> None:
        self.state = AgentState(session_id="s7-session")
        self.state.context = _conversation()
        self.context_config = ContextConfig(trigger_ratio=0.6, reserve_ratio=0.1)
        self.model = FakeModel(token_count)
        self.after_compress_tokens = after_compress_tokens
        self.compress_calls: list[ContextConfig | None] = []

    async def _prepare_model_input(self) -> dict:
        return {
            "messages": [SystemMsg(name="system", content="system prompt"), *self.state.context],
            "tools": [],
        }

    async def compress_context(self, context_config=None, instructions=None) -> None:
        self.compress_calls.append(context_config)
        if self.after_compress_tokens is not None:
            self.model.tokens = self.after_compress_tokens
            self.state.context = [UserMsg(name="user", content="compressed")]
            self.state.summary = "compressed"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# archive: old messages survive as an inspectable file + pointer
# ---------------------------------------------------------------------------


def test_offload_context_writes_json_and_returns_pointer(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)

    ref = _run(offloader.offload_context("s7-session", _conversation()))

    assert ref == "archive://s7-session/0001"
    files = list((tmp_path / "s7-session").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["archive_id"] == ref
    assert payload["session_id"] == "s7-session"
    assert payload["message_count"] == 6
    assert payload["messages"][0]["content"][0]["text"] == "给猫买个饮水器"
    # The card is preserved verbatim, not summarized away.
    assert "P3472-S1" in files[0].read_text(encoding="utf-8")


def test_offload_context_uses_a_new_sequence_per_batch(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)

    first = _run(offloader.offload_context("s7-session", _conversation()))
    second = _run(offloader.offload_context("s7-session", _conversation()))

    assert first == "archive://s7-session/0001"
    assert second == "archive://s7-session/0002"


def test_offload_context_sanitizes_the_session_component(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)

    ref = _run(offloader.offload_context("../../etc/passwd", _conversation()))

    # The pointer stays inside the archive root: no ``..`` and no ``/`` escape.
    assert ref.startswith("archive://")
    assert ".." not in ref.split("/")[-2]
    assert len(list(tmp_path.iterdir())) == 1
    assert all(tmp_path.resolve() in path.resolve().parents for path in tmp_path.rglob("*.json"))


def test_read_archive_returns_model_readable_messages(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    ref = _run(offloader.offload_context("s7-session", _conversation()))

    rendered = json.loads(offloader.read(ref))

    assert rendered["archive_id"] == ref
    assert rendered["message_count"] == 6
    assert rendered["messages"][0]["role"] == "user"
    assert rendered["messages"][0]["text"] == "给猫买个饮水器"
    assert rendered["messages"][3]["tool_calls"][0]["name"] == "GenerateStructuredOutput"


def test_read_archive_unknown_pointer_returns_error_not_raise(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)

    rendered = json.loads(offloader.read("archive://nobody/9999"))

    assert "error" in rendered


def test_offload_tool_result_writes_its_own_file(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    block = ToolResultBlock(
        id="call-1",
        name="product_search",
        output="x" * 100,
        state=ToolResultState.SUCCESS,
    )

    ref = _run(offloader.offload_tool_result("s7-session", block))

    assert ref == "archive://s7-session/0001"
    payload = json.loads(next((tmp_path / "s7-session").glob("*.json")).read_text())
    assert payload["tool_result"]["id"] == "call-1"


# ---------------------------------------------------------------------------
# fact extraction / summary validation
# ---------------------------------------------------------------------------


def test_product_facts_reads_the_structured_output_tool_call() -> None:
    facts = product_facts([_cards_message()])

    assert [(f.sku_id, f.price_major, f.currency) for f in facts] == [("P3472-S1", 567.0, "CNY")]


def test_product_facts_also_reads_the_message_structured_output_shape() -> None:
    msg = Msg(
        name="purchase_agent",
        role="assistant",
        content=[TextBlock(text="done")],
        structured_output={"cards": [{"sku_id": "P1-S1", "price_major": 20.0}]},
    )

    assert [f.sku_id for f in product_facts([msg])] == ["P1-S1"]


def test_validate_summary_rejects_an_invented_sku() -> None:
    violations = validate_summary(
        "我之前推荐过 P9999-S1，可以看看",
        source=[_cards_message()],
        retained=[],
        previous_summary="",
    )

    assert violations
    assert "P9999-S1" in violations[0]


def test_validate_summary_rejects_a_misquoted_price() -> None:
    violations = validate_summary(
        "推荐 P3472-S1，价格 999 元",
        source=[_cards_message()],
        retained=[],
        previous_summary="",
    )

    assert violations
    assert "567" in violations[0]


def test_validate_summary_accepts_a_faithful_summary() -> None:
    violations = validate_summary(
        "之前推荐过 P3472-S1，价格 567.00 CNY",
        source=[_cards_message()],
        retained=[],
        previous_summary="",
    )

    assert violations == []


def test_validate_summary_allows_facts_carried_by_the_previous_summary() -> None:
    # The SKU is no longer in the source (it was summarized one round earlier);
    # repeating it is legitimate, not an invention.
    violations = validate_summary(
        "更早推荐过 P1111-S1",
        source=[_cards_message()],
        retained=[],
        previous_summary="此前推荐 P1111-S1。",
    )

    assert violations == []


def test_validate_summary_rejects_an_invented_non_catalog_sku() -> None:
    # The model can emit its own id shape (a real run produced ``sku_2175_b01``).
    # Validation must not depend on the catalog's ``P<digits>-S<digits>`` form.
    violations = validate_summary(
        "之前推荐过 sku_9999_x01，可以下单",
        source=[_cards_message(sku="sku_2175_b01")],
        retained=[],
        previous_summary="",
    )

    assert violations
    assert "sku_9999_x01" in violations[0]


def test_validate_summary_accepts_a_non_catalog_sku_from_the_card() -> None:
    violations = validate_summary(
        "推荐 sku_2175_b01，价格 567.00 CNY",
        source=[_cards_message(sku="sku_2175_b01")],
        retained=[],
        previous_summary="",
    )

    assert violations == []


def test_appendix_round_trips_a_non_catalog_sku() -> None:
    facts = [ProductFact("sku_2175_b01", "Atlas 宠物出行包 175", 89.0, "CNY")]

    once = append_protected_facts("模型摘要", facts)
    assert facts_from_appendix(once) == facts

    # A second compression only sees the previous appendix: the fact must survive.
    twice = append_protected_facts("新的摘要", facts_from_appendix(once))
    assert isinstance(twice, str)
    assert "- sku_2175_b01 | Atlas 宠物出行包 175 | ¥89.00 CNY" in twice


def test_preference_facts_skips_questions() -> None:
    messages = [UserMsg(name="user", content="我预算多少？刚才推荐的 product_id 是哪个？")]

    assert preference_facts(messages) == []


def test_preference_facts_keeps_a_statement_with_a_cue() -> None:
    messages = [UserMsg(name="user", content="预算 2000 以内")]

    assert preference_facts(messages) == ["预算 2000 以内"]


def test_preference_facts_dedupes_by_semantic_key_not_by_prefix() -> None:
    messages = [
        UserMsg(name="user", content=f"第{index}轮：再帮我比较一下猫用的出行包，预算 2000 以内。")
        for index in range(1, 5)
    ]

    preferences = preference_facts(messages)

    assert len(preferences) == 1
    assert "预算 2000 以内" in preferences[0]


def test_preference_facts_is_capped_keeping_the_most_recent() -> None:
    messages = [
        UserMsg(name="user", content=f"预算 {index}000 元以内，要轻一点。")
        for index in range(1, 41)
    ]

    preferences = preference_facts(messages)

    assert len(preferences) == MAX_PREFERENCES
    assert "40000" in preferences[-1]


def test_append_protected_facts_applies_the_same_preference_hygiene() -> None:
    summary = append_protected_facts(
        "摘要",
        [],
        ["第1轮：预算 2000 以内", "第2轮：预算 2000 以内", "我预算多少", "不要皮革"],
    )

    assert isinstance(summary, str)
    assert summary.count("预算 2000 以内") == 1
    assert "我预算多少" not in summary
    assert "不要皮革" in summary


def test_append_protected_facts_is_idempotent_and_prices_are_verbatim() -> None:
    once = append_protected_facts("模型摘要", product_facts([_cards_message()]))
    twice = append_protected_facts(once, product_facts([_cards_message()]))

    assert once.count(PROTECTED_FACTS_HEADER) == 1
    assert twice == once
    assert "P3472-S1" in once
    assert "567.00" in once
    assert "CNY" in once


def test_append_protected_facts_handles_a_block_summary() -> None:
    summary = [TextBlock(text="模型摘要")]

    result = append_protected_facts(summary, product_facts([_cards_message()]))

    assert isinstance(result, list)
    assert "P3472-S1" in result[-1].text


# ---------------------------------------------------------------------------
# on_compress_context: protect the tail, validate, append facts, roll back
# ---------------------------------------------------------------------------


def _compressor(offloader: SessionArchiveOffloader, *, keep: int = 2, summary: str = "摘要"):
    async def compress(**kwargs):
        agent = kwargs["agent"]
        cut = agent.state.context[:-keep]
        await offloader.offload_context(agent.state.session_id, cut)
        agent.state.summary = summary
        agent.state.context = agent.state.context[-keep:]

    return compress


def test_compress_disables_truncation_fallback_and_reserves_the_tail(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    seen: list[ContextConfig] = []

    async def next_handler(**kwargs):
        seen.append(kwargs["context_config"])
        await _compressor(offloader)(agent=agent, **kwargs)

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    cfg = seen[0]
    assert cfg.compression_fallback_to_truncation is False
    # Two protected turns must be a real reserve, not the default 10%.
    assert cfg.reserve_ratio > 0.1


def test_compress_appends_protected_facts_from_the_archived_messages(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()

    async def next_handler(**kwargs):
        # The last two turns (4 messages: u2, cards, u3, tool result) stay.
        await _compressor(offloader, keep=4, summary="用户在看饮水器")(agent=agent, **kwargs)

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert PROTECTED_FACTS_HEADER in agent.state.summary
    assert "P3472-S1" in agent.state.summary
    assert "567.00" in agent.state.summary


def test_compress_rolls_back_when_the_summary_invents_a_sku(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    before_context = list(agent.state.context)

    async def next_handler(**kwargs):
        await _compressor(offloader, keep=4, summary="我之前推荐过 P9999-S1")(agent=agent, **kwargs)

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert agent.state.context == before_context
    assert agent.state.summary == ""
    assert offloader.last_batch is None


def test_compress_rolls_back_when_the_summary_fails(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    before_context = list(agent.state.context)

    async def next_handler(**kwargs):
        raise RuntimeError("summary model exploded")

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert agent.state.context == before_context
    assert agent.state.summary == ""


def test_compress_rolls_back_when_the_protected_tail_was_dropped(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    before_context = list(agent.state.context)

    async def next_handler(**kwargs):
        # Simulate a bad split: only the oldest turn survives.
        await _compressor(offloader, keep=1, summary="摘要")(agent=agent, **kwargs)
        agent.state.context = before_context[:2]

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert agent.state.context == before_context


def test_compress_without_an_archive_is_a_no_op(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()

    async def next_handler(**kwargs):
        return None  # below the trigger threshold: nothing compressed

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert agent.state.summary == ""
    assert len(agent.state.context) == 6


# ---------------------------------------------------------------------------
# on_model_call: the token ceiling is a gate, not advice
# ---------------------------------------------------------------------------


def _call(middleware, agent, messages=None):
    calls: list[dict] = []

    async def next_handler(**kwargs):
        calls.append(kwargs)
        return "model-response"

    result = _run(
        middleware.on_model_call(
            agent=agent,
            input_kwargs={
                "current_model": agent.model,
                # Production always carries a system prompt; tests must too,
                # because preferences ride on it rather than on the tail.
                "messages": messages
                or [
                    SystemMsg(name="system", content="system prompt"),
                    UserMsg(name="user", content="hi"),
                ],
                "tools": [],
                "tool_choice": None,
            },
            next_handler=next_handler,
        )
    )
    return result, calls


def test_model_call_under_the_gate_is_forwarded_untouched(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent(token_count=500)
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)

    result, calls = _call(middleware, agent)

    assert result == "model-response"
    assert len(calls) == 1
    assert agent.compress_calls == []


def test_model_call_over_the_gate_forces_compression_then_forwards(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent(token_count=900, after_compress_tokens=400)
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)

    result, calls = _call(middleware, agent)

    assert result == "model-response"
    assert len(agent.compress_calls) == 1
    # The forwarded input is the *post-compression* one, not the oversized original.
    assert [m.get_text_content() for m in calls[0]["messages"]] == ["system prompt", "compressed"]


def test_model_call_still_over_the_gate_raises_and_never_calls_the_model(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent(token_count=900, after_compress_tokens=900)
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)

    try:
        _call(middleware, agent)
    except ContextCapacityError as exc:
        assert exc.tokens == 900
        assert exc.limit == 850
    else:  # pragma: no cover - the assertion below is the real check
        raise AssertionError("capacity gate did not trip")


def test_model_call_fails_fast_on_retry_after_the_gate_tripped(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent(token_count=900, after_compress_tokens=900)
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)

    try:
        _call(middleware, agent)
    except ContextCapacityError:
        pass
    compressed_once = len(agent.compress_calls)

    try:
        _call(middleware, agent)
    except ContextCapacityError:
        pass

    assert len(agent.compress_calls) == compressed_once  # no second expensive compression


def test_on_reply_resets_the_gate_flag_for_the_next_turn(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent(token_count=900, after_compress_tokens=900)
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)

    try:
        _call(middleware, agent)
    except ContextCapacityError:
        pass

    async def reply_chain(**kwargs):
        yield "event"

    events = _run(_collect(middleware.on_reply(agent, {}, reply_chain)))

    assert list(events) == ["event"]
    assert middleware._gate_tripped is False


async def _collect(gen):
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# wiring: run_turn + router surface the capacity error as 507
# ---------------------------------------------------------------------------


class _CapacityAgent:
    def __init__(self) -> None:
        self.state = AgentState()

    async def reply(self, msg, structured_schema=None) -> Msg:
        raise ContextCapacityError(tokens=900, limit=850)


def test_run_turn_propagates_capacity_error_without_wrapping_it() -> None:
    store = SessionStore(agent_factory=lambda state=None: _CapacityAgent())

    try:
        _run(run_turn(store, "u1", "s1", "你好"))
    except ContextCapacityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("capacity error was swallowed into ModelFailureError")


def test_router_maps_capacity_error_to_507() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/commerce")
    app.state.chat_store = SessionStore(agent_factory=lambda state=None: _CapacityAgent())
    app.state.session_repository = None
    # A short turn budget keeps this test fast; S11's guard timer (20s) would
    # not fit inside it, so the guardrails are off for this fake-agent turn.
    app.state.settings = Settings(
        _env_file=None,
        turn_timeout_seconds=10.0,
        dispatch_enabled=False,
        guardrails_enabled=False,
    )

    with TestClient(app) as client:
        resp = client.post(
            "/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "你好"},
        )

    assert resp.status_code == 507
    assert resp.json()["error"]["message"] == "上下文超出容量闸门"


# ---------------------------------------------------------------------------
# the turn counter must survive compression (S6's derivation no longer holds)
# ---------------------------------------------------------------------------


def test_on_reply_counts_turns_and_the_count_outlives_compression(tmp_path: Path) -> None:
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)

    async def reply_chain(**kwargs):
        yield "event"

    _run(_collect(middleware.on_reply(agent, {}, reply_chain)))
    _run(_collect(middleware.on_reply(agent, {}, reply_chain)))
    # Seeded from the three user messages already in the context.
    assert recorded_turn_count(agent.state) == 5

    agent.state.context = []  # what compression does to older turns
    assert recorded_turn_count(agent.state) == 5


def test_compress_rolls_back_when_a_protected_message_was_partially_split(
    tmp_path: Path,
) -> None:
    """Keeping the message id is not enough: its blocks must survive too."""
    offloader = SessionArchiveOffloader(tmp_path)
    agent = FakeAgent()
    before_context = list(agent.state.context)
    protected_card = before_context[3]  # the assistant message carrying the cards

    async def next_handler(**kwargs):
        await _compressor(offloader, keep=4, summary="摘要")(agent=agent, **kwargs)
        # Simulate the block-level boundary split: the id survives, the blocks
        # do not. Work on copies so the "before" snapshot stays pristine.
        agent.state.context = [copy.deepcopy(m) for m in before_context[2:]]
        agent.state.context[1].content = []

    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    _run(
        middleware.on_compress_context(
            agent=agent,
            input_kwargs={"context_config": agent.context_config, "instructions": None},
            next_handler=next_handler,
        )
    )

    assert agent.state.context == before_context
    assert protected_card.content  # restored with its blocks


# ---------------------------------------------------------------------------
# integration with the real AgentScope compression pipeline (fake model, no API)
# ---------------------------------------------------------------------------


class _SummaryModel:
    """Duck-typed chat model: real compression calls, canned summary."""

    def __init__(self, context_size: int, summary: dict) -> None:
        self.context_size = context_size
        self.summary = summary
        self.summary_calls: list[list] = []

    async def count_tokens(self, messages, tools=None) -> int:
        size = sum(len(json.dumps(m.model_dump(mode="json"), ensure_ascii=False)) for m in messages)
        return size // 4 + 1

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        self.summary_calls.append(messages)
        return StructuredResponse(content=self.summary)


_SUMMARY_TEMPLATE_VALUES = {
    "task_overview": "用户要买宠物用品",
    "current_state": "已推荐过商品",
    "important_discoveries": "",
    "next_steps": "",
    "context_to_preserve": "",
}


def _long_turn(label: str, role: str = "user") -> Msg:
    body = f"{label} " + ("补" * 900)
    if role == "user":
        return UserMsg(name="user", content=body)
    return AssistantMsg(name="purchase_agent", content=[TextBlock(text=body)])


def _real_agent(tmp_path: Path, summary: dict) -> tuple[object, SessionArchiveOffloader]:
    offloader = SessionArchiveOffloader(tmp_path)
    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    agent = Agent(
        name="purchase_agent",
        system_prompt="You are a purchase agent.",
        model=_SummaryModel(context_size=4000, summary=summary),
        toolkit=Toolkit(),
        middlewares=[middleware],
        offloader=offloader,
        context_config=ContextConfig(
            trigger_ratio=0.6,
            reserve_ratio=0.1,
            compression_fallback_to_truncation=False,
        ),
    )
    agent.state.context = [
        _long_turn("第1轮问题"),
        _long_turn("第1轮回答", role="assistant"),
        _long_turn("第2轮问题"),
        _cards_message(),
        _long_turn("第2轮回答", role="assistant"),
        _long_turn("第3轮问题"),
        _long_turn("第3轮回答", role="assistant"),
        _long_turn("第4轮问题"),
        _long_turn("第4轮回答", role="assistant"),
    ]
    return agent, offloader


def test_real_compression_archives_and_appends_facts(tmp_path: Path) -> None:
    agent, offloader = _real_agent(tmp_path, dict(_SUMMARY_TEMPLATE_VALUES))

    _run(agent.compress_context())

    assert offloader.last_batch is not None
    archives = list(tmp_path.glob("*/*.json"))
    assert len(archives) == 1
    assert PROTECTED_FACTS_HEADER in agent.state.summary
    assert "P3472-S1" in agent.state.summary
    assert "567.00" in agent.state.summary
    # The real split kept the last two turns (u2/a2/u3/a3 — indices 5..8).
    kept_text = "\n".join(m.get_text_content() or "" for m in agent.state.context)
    assert "第3轮问题" in kept_text
    assert "第4轮问题" in kept_text


def test_real_compression_rolls_back_an_invented_sku(tmp_path: Path) -> None:
    summary = dict(_SUMMARY_TEMPLATE_VALUES)
    summary["current_state"] = "之前推荐过 P9999-S1"
    agent, offloader = _real_agent(tmp_path, summary)
    before = list(agent.state.context)

    _run(agent.compress_context())

    assert agent.state.context == before
    assert agent.state.summary == ""
    assert offloader.last_batch is None


# ---------------------------------------------------------------------------
# S8: three-scope preference injection
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _s8_memory(tmp_path: Path) -> PreferenceMemory:
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    return PreferenceMemory(buyer_id="b1", store=store, embedder=_FakeEmbedder(), vector_model="m1")


async def _seed_scopes(memory: PreferenceMemory, state: AgentState) -> None:
    await memory.begin_turn(state)
    await memory.add(state, "long_term", "预算 500 以内")
    await memory.add(state, "turn", "这次先看看 2000 以内的")
    await memory.add(state, "session", "只考虑母婴宠物类目")


def _injected_text(middleware, agent) -> str:
    _, calls = _call(middleware, agent)
    messages = calls[0]["messages"]
    system, last = messages[0], messages[-1]
    assert system.role == "system"
    # The user's own message must remain the *last* one: a preference block at
    # the tail makes historical constraints look like the current request.
    assert last.role == "user" and last.get_text_content() == "hi"
    return system.get_text_content() or ""


def test_injection_renders_three_scopes_in_order(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    _run(_seed_scopes(memory, agent.state))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    turn = memory.turn_items()[0]
    session = memory.session_items(agent.state)[0]
    long_term = memory.long_term_items()[0]
    assert f"[本轮] {turn.id} {turn.text}" in text
    assert f"[本会话] {session.id} {session.text}" in text
    assert f"[长期] {long_term.id} {long_term.text}" in text
    assert text.index("[本轮]") < text.index("[本会话]") < text.index("[长期]")


def test_injection_omits_empty_scopes(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    # No transcript constraints either: the heuristic must not invent a section.
    agent.state.context = []
    _run(memory.begin_turn(agent.state))
    _run(memory.add(agent.state, "long_term", "预算 500 以内"))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)
    section_lines = [line for line in text.splitlines() if line.startswith("[")]

    assert len(section_lines) == 1
    assert section_lines[0].startswith("[长期] ")


def test_injection_falls_back_to_s7_when_no_memory(tmp_path: Path) -> None:
    agent = FakeAgent()
    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(tmp_path))

    text = _injected_text(middleware, agent)

    # FakeAgent's conversation contains "预算 600", which the S7 heuristic keeps.
    assert "会话偏好" in text
    assert "预算 600" in text
    assert "[本轮]" not in text


def test_injection_keeps_the_current_user_message_last(tmp_path: Path) -> None:
    agent = FakeAgent()
    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(tmp_path))

    _, calls = _call(
        middleware,
        agent,
        [SystemMsg(name="system", content="system prompt"), UserMsg(name="user", content="你好")],
    )
    messages = calls[0]["messages"]

    assert [message.role for message in messages] == ["system", "user"]
    assert messages[-1].get_text_content() == "你好"


def test_injection_does_not_add_a_message(tmp_path: Path) -> None:
    agent = FakeAgent()
    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(tmp_path))

    _, calls = _call(middleware, agent)

    assert len(calls[0]["messages"]) == 2


def test_injection_prepends_a_system_message_when_none_exists(tmp_path: Path) -> None:
    agent = FakeAgent()
    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(tmp_path))

    _, calls = _call(middleware, agent, [UserMsg(name="user", content="hi")])
    messages = calls[0]["messages"]

    assert messages[0].role == "system"
    assert "预算 600" in (messages[0].get_text_content() or "")
    assert messages[-1].get_text_content() == "hi"


def test_injection_does_not_touch_state_context(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    _run(_seed_scopes(memory, agent.state))
    before = list(agent.state.context)
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    assert agent.state.context == before
    assert "只考虑母婴宠物类目" not in "\n".join(
        message.get_text_content() or "" for message in agent.state.context
    )
    assert text  # the constraint lives only in the injected message


def test_begin_turn_clears_only_turn_scope(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    _run(_seed_scopes(memory, agent.state))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    async def reply_chain(**kwargs):
        yield "event"

    _run(_collect(middleware.on_reply(agent, {}, reply_chain)))

    assert memory.turn_items() == []
    assert [item.text for item in memory.session_items(agent.state)] == ["只考虑母婴宠物类目"]
    assert [item.text for item in memory.long_term_items()] == ["预算 500 以内"]


def test_injection_lists_only_explicit_session_records(tmp_path: Path) -> None:
    """A one-off query in the transcript is not a session preference.

    ``preference_facts`` scans every historical user message for cue words, so
    "300元以内保温杯" would otherwise be re-injected forever — and, because it
    rides at the tail, read by the model as the current request.
    """
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    _run(memory.begin_turn(agent.state))
    session = _run(memory.add(agent.state, "session", "只考虑母婴宠物类目"))
    agent.state.context.append(UserMsg(name="user", content="300元以内保温杯，不要塑料"))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)
    session_lines = [line for line in text.splitlines() if line.startswith("[本会话]")]

    assert session_lines == [f"[本会话] {session.preference.id} 只考虑母婴宠物类目"]
    assert "保温杯" not in text


def test_injection_with_memory_does_not_fall_back_to_the_heuristic(tmp_path: Path) -> None:
    """With S8 wired, an empty memory means "say nothing", not "mine the chat"."""
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()  # its transcript says "预算 600"
    _run(memory.begin_turn(agent.state))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    _, calls = _call(middleware, agent)
    messages = calls[0]["messages"]

    assert len(messages) == 2
    assert "预算 600" not in (messages[0].get_text_content() or "")


def test_injection_names_the_operations_still_awaiting_confirmation(tmp_path: Path) -> None:
    """S8-6: an unanswered add is not a memory, but it is not lost either."""
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    agent.state.context = []
    _run(memory.begin_turn(agent.state))
    pending = _run(memory.stage(agent.state, "long_term", "我以后只看官方店"))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    assert "还有待确认的记忆操作" in text
    assert f"[待确认] {pending.id} scope=long_term" in text
    # Nothing was written: there is no scope section to show.
    assert "[长期]" not in text


def test_a_pending_operation_is_still_named_after_a_new_turn(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    _run(memory.begin_turn(agent.state))
    pending = _run(memory.stage(agent.state, "session", "只考虑母婴宠物类目"))

    _run(memory.begin_turn(agent.state))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    assert f"[待确认] {pending.id} scope=session" in text


def test_injection_labels_a_dislike_with_its_materials(tmp_path: Path) -> None:
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    agent.state.context = []
    _run(memory.begin_turn(agent.state))
    added = _run(
        memory.add(
            agent.state, "long_term", "不要塑料的", kind="dislike", material_tags=("塑料", "玻璃")
        )
    )
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    assert f"[长期·不喜欢] {added.preference.id} 不要塑料的（材质：塑料、玻璃）" in text


def test_long_term_lines_carry_their_version(tmp_path: Path) -> None:
    """S8-4: the model cannot send ``expected_version`` it never saw."""
    memory = _s8_memory(tmp_path)
    agent = FakeAgent()
    agent.state.context = []
    _run(memory.begin_turn(agent.state))
    added = _run(memory.add(agent.state, "long_term", "预算 500 以内"))
    middleware = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(tmp_path), preferences=memory
    )

    text = _injected_text(middleware, agent)

    assert f"[长期] {added.preference.id} 预算 500 以内（v1）" in text
