"""S13 acceptance suite: trace, metrics and events end to end."""

import asyncio
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from qdrant_client import QdrantClient

import harbor.agents.factory as factory
from harbor.agents.factory import build_agent, build_runtime
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.catalog.search.service import CatalogSearchService
from harbor.features.chat.service import ModelFailureError, SessionStore, run_turn
from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.runtime import GuardRuntime
from harbor.observability import Observability, configure_observability, trace_scope

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"


@pytest.fixture()
def obs():
    instance = Observability.create()
    previous = configure_observability(instance)
    yield instance
    configure_observability(previous)


class _State:
    def __init__(self) -> None:
        self.context: list = []


class _ReplyAgent:
    def __init__(self, summary: str) -> None:
        self.state = _State()
        self.summary = summary

    async def reply(self, message, structured_schema=None):
        reply = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text=self.summary)],
            structured_output={"cards": [], "summary": self.summary},
        )
        self.state.context.append(reply)
        return reply


class _BoomAgent:
    def __init__(self) -> None:
        self.state = _State()
        self.calls = 0

    async def reply(self, message, structured_schema=None):
        self.calls += 1
        raise RuntimeError("planner exploded")


class _NeverAgent:
    def __init__(self) -> None:
        self.state = _State()
        self.calls = 0

    async def reply(self, message, structured_schema=None):
        self.calls += 1
        raise AssertionError("the agent must not run on a budget short circuit")


class _FakeModel(ChatModelBase):
    def __init__(self, model: str, script: list[str]) -> None:
        super().__init__(credential=None, model=model, parameters=None, max_retries=0)
        self.script = list(script)
        self.calls = 0

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        return ChatResponse(content=[TextBlock(text=self.script.pop(0))], is_last=True)


class _FakeEmbedder:
    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    def embed(self, texts):
        vectors = []
        for text in texts:
            counts = [0.0] * self._dim
            for char in text:
                counts[ord(char) % self._dim] += 1.0
            norm = sum(value * value for value in counts) ** 0.5 or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


class _RecordingService:
    def search(self, query, spec, limit=10):
        return SearchResult(hits=[], total=0)


def _product(pid: str, title: str) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=title,
        brand="TestBrand",
        category="家用电器",
        tax_category="家用电器",
        description="",
        highlights=[{"label": "卖点", "detail": "轻便"}],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=0.5,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{pid}-S1",
                spec="标准",
                price=Money.from_major_units(100.0, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def _messages():
    return [Msg(name="user", role="user", content=[TextBlock(text="hi")])]


def _guard(tmp_path, cap: int = 1000) -> GuardRuntime:
    metrics = GuardMetrics()
    return GuardRuntime(
        budget=SqliteTokenBudget(tmp_path / "budget.sqlite3", cap),
        circuits=CircuitBreaker(3, 60.0, clock=lambda: 0.0, metrics=metrics),
        metrics=metrics,
    )


# --------------------------------------------------------------------------
# S13-1: trace answers "where did the time go?"
# --------------------------------------------------------------------------


def test_s13_01_turn_trace_attributes_the_time(obs) -> None:
    agent = _ReplyAgent("找到 1 件商品")
    store = SessionStore(agent_factory=lambda state=None: agent)

    response = asyncio.run(run_turn(store, "b1", "s1", "找台灯", timeout_seconds=5))

    trace = obs.traces.recent()[0]
    names = [item.name for item in trace.spans]
    assert names[0] == "turn"
    assert "agent.reply" in names
    assert "turn.persist" in names
    assert trace.attributes["session_id"] == "s1"
    assert trace.attributes["buyer_id"] == "b1"
    assert trace.status == "ok"
    assert trace.spans[0].duration_ms >= trace.spans[names.index("agent.reply")].duration_ms
    assert response.final_text == "找到 1 件商品"


def test_s13_02_failed_turn_is_marked_and_visible(obs) -> None:
    agent = _BoomAgent()
    store = SessionStore(agent_factory=lambda state=None: agent)

    with pytest.raises(ModelFailureError):
        asyncio.run(run_turn(store, "b1", "s1", "你好", timeout_seconds=5))

    trace = obs.traces.recent()[0]
    assert trace.status == "error"
    assert trace.spans[0].status == "error"
    events = obs.events.recent(session_id="s1")
    assert [event.type for event in events] == ["turn.started", "turn.failed"]
    # The buyer-visible stream carries a stable code, never raw exception text
    # (which can contain filesystem paths or provider internals).
    assert events[-1].data["error"] == "ModelFailureError"


def test_s13_02b_turn_keeps_a_joined_http_trace_findable_by_session(obs) -> None:
    # In production the middleware owns the trace and the turn joins it; the
    # session filter must still find that trace.
    agent = _ReplyAgent("好的")
    store = SessionStore(agent_factory=lambda state=None: agent)

    async def scenario():
        with trace_scope("http.request", path="/commerce/intents"):
            return await run_turn(store, "b1", "s1", "你好", timeout_seconds=5)

    asyncio.run(scenario())

    traces = obs.traces.recent(session_id="s1")
    assert len(traces) == 1
    assert traces[0].name == "http.request"
    assert [item.name for item in traces[0].spans][:2] == ["http.request", "turn"]


def test_s13_03_model_calls_are_spans(obs, tmp_path) -> None:
    model = _FakeModel("main", ["ok"])
    guarded = GuardedChatModel(
        model, _guard(tmp_path), request_timeout=1, max_retries=0, backoff=0.0
    )

    asyncio.run(guarded(_messages()))

    trace = obs.traces.recent()[0]
    assert trace.spans[0].name == "model.call"
    assert trace.spans[0].attributes["model"] == "main"
    assert trace.spans[0].attributes["tier"] == "main"
    assert obs.metrics.snapshot()["histograms"]["span.model.call.latency_ms"]["count"] == 1


def test_s13_04_search_tool_and_retrieval_are_spans(obs) -> None:
    service = CatalogSearchService([_product("P1", "无线吸尘器 轻便")], _FakeEmbedder())
    tool = make_product_search_tool(service)

    asyncio.run(tool._func(query="吸尘器", category="家用电器"))  # type: ignore[attr-defined]

    trace = obs.traces.recent()[0]
    names = [item.name for item in trace.spans]
    assert names[0] == "tool.product_search"
    assert "search.retrieve" in names
    assert trace.spans[0].attributes["hits"] >= 1
    retrieval = trace.spans[names.index("search.retrieve")]
    assert retrieval.parent_id == trace.spans[0].span_id

    counters = obs.metrics.snapshot()["counters"]
    assert counters["tool.search.calls"] == 1
    events = obs.events.recent()
    assert events[-1].type == "tool.search.completed"
    assert events[-1].data["recall_strategy"] in ("embedding", "keyword_2gram")


def test_s13_05_empty_search_is_counted() -> None:
    obs = Observability.create()
    previous = configure_observability(obs)
    try:
        tool = make_product_search_tool(_RecordingService())
        asyncio.run(tool._func(query="不存在的东西"))  # type: ignore[attr-defined]
        counters = obs.metrics.snapshot()["counters"]
        assert counters["tool.search.empty"] == 1
    finally:
        configure_observability(previous)


# --------------------------------------------------------------------------
# S13-2 / S13-3: metrics and the user-visible event stream
# --------------------------------------------------------------------------


def test_s13_06_turn_metrics_and_answer_event(obs) -> None:
    agent = _ReplyAgent("找到 1 件商品")
    store = SessionStore(agent_factory=lambda state=None: agent)

    asyncio.run(run_turn(store, "b1", "s1", "找台灯", timeout_seconds=5))

    histograms = obs.metrics.snapshot()["histograms"]
    assert histograms["span.turn.latency_ms"]["count"] == 1
    assert histograms["span.agent.reply.latency_ms"]["count"] == 1

    events = obs.events.recent(session_id="s1")
    assert [event.type for event in events] == ["turn.started", "turn.answer_ready"]
    answer = events[-1]
    assert answer.data["final_text"] == "找到 1 件商品"
    assert answer.data["card_count"] == 0
    assert answer.trace_id == obs.traces.recent()[0].trace_id


def test_s13_07_budget_short_circuit_is_visible(obs, tmp_path) -> None:
    guard = _guard(tmp_path, cap=10)
    assert guard.budget.acquire(10).granted
    store = SessionStore(agent_factory=lambda state=None: _NeverAgent(), guard=guard)

    response = asyncio.run(run_turn(store, "b1", "s1", "你好", timeout_seconds=5))

    assert "额度" in response.final_text
    events = obs.events.recent(session_id="s1")
    assert [event.type for event in events] == [
        "turn.started",
        "turn.short_circuit",
        "turn.answer_ready",
    ]
    assert events[1].data["reason"] == "turn_fallback"


# --------------------------------------------------------------------------
# Checklist S13-1 / S13-2 / S13-5
# --------------------------------------------------------------------------


class _FakeEmbedder:
    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


class _Formatter:
    supported_input_media_types = ("image/*", "audio/*", "video/*")


class _ToolCallingModel(ChatModelBase):
    """First call asks for product_search, the second answers."""

    def __init__(self) -> None:
        super().__init__(credential=None, model="fake-main", parameters=None, max_retries=0)
        self.formatter = _Formatter()
        self.calls = 0

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="call-1",
                        name="product_search",
                        input=json.dumps({"query": "台灯", "category": "家用电器"}),
                    )
                ],
                is_last=True,
                usage=ChatUsage(input_tokens=50, output_tokens=10, time=0.01),
            )
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id="call-2",
                    name="GenerateStructuredOutput",
                    input=json.dumps({"cards": [], "summary": "找到了"}),
                )
            ],
            is_last=True,
            usage=ChatUsage(input_tokens=80, output_tokens=5, time=0.01),
        )


def _fake_runtime(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, openai_api_key="sk-test")
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: _FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-s13")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    return build_runtime(settings, data_path=FIXTURE)


def test_s13_01_one_request_shows_api_agent_tool_and_model(monkeypatch, tmp_path, obs) -> None:
    runtime = _fake_runtime(monkeypatch, tmp_path)
    model = GuardedChatModel(
        _ToolCallingModel(),
        _guard(tmp_path, cap=1_000_000),
        request_timeout=5,
        max_retries=0,
        backoff=0.0,
    )
    agent = build_agent(replace(runtime, model=model))
    store = SessionStore(agent_factory=lambda state=None: agent)

    async def scenario():
        with trace_scope("http.request", path="/commerce/intents"):
            return await run_turn(store, "b1", "s1", "找台灯", timeout_seconds=10)

    asyncio.run(scenario())

    trace = obs.traces.recent()[0]
    by_name = {}
    for span in trace.spans:
        by_name.setdefault(span.name, span)

    # API → Agent → tool, and API → Agent → model, all in one trace.
    assert list(by_name)[0] == "http.request"
    assert by_name["turn"].parent_id == by_name["http.request"].span_id
    assert by_name["agent.reply"].parent_id == by_name["turn"].span_id
    assert by_name["tool.product_search"].parent_id == by_name["agent.reply"].span_id
    assert by_name["model.call"].parent_id == by_name["agent.reply"].span_id


def test_s13_02_the_trace_names_the_tool_and_its_time(monkeypatch, tmp_path, obs) -> None:
    runtime = _fake_runtime(monkeypatch, tmp_path)
    model = GuardedChatModel(
        _ToolCallingModel(),
        _guard(tmp_path, cap=1_000_000),
        request_timeout=5,
        max_retries=0,
        backoff=0.0,
    )
    agent = build_agent(replace(runtime, model=model))
    store = SessionStore(agent_factory=lambda state=None: agent)

    asyncio.run(run_turn(store, "b1", "s1", "找台灯", timeout_seconds=10))

    trace = obs.traces.recent()[0]
    tool_spans = [span for span in trace.spans if span.name.startswith("tool.")]
    assert [span.name for span in tool_spans] == ["tool.product_search"]
    assert tool_spans[0].duration_ms is not None
    assert tool_spans[0].attributes["query_chars"] == len("台灯")
    assert tool_spans[0].attributes["hits"] >= 0

    # The same attribution is available as a metric, so a regression in one
    # tool is visible without opening individual traces.
    histogram = obs.metrics.snapshot()["histograms"]["span.tool.product_search.latency_ms"]
    assert histogram["count"] == 1


def test_s13_05_events_subscribe_per_session_and_replay_in_order(obs) -> None:
    for index in range(3):
        obs.events.publish("turn.started", session_id="s1", index=index)
    obs.events.publish("turn.started", session_id="s2")

    async def scenario():
        subscription = obs.events.subscribe(session_id="s1", after_seq=1)
        replayed = [await subscription.get(timeout=0.2) for _ in range(2)]
        subscription.close()
        return replayed

    events = asyncio.run(scenario())

    assert [event.seq for event in events] == [2, 3]
    assert [event.data["index"] for event in events] == [1, 2]
    assert all(event.session_id == "s1" for event in events)
