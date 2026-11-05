"""S9: task_dispatch contracts, evidence verification, and batch execution."""

import asyncio
import json
import time
from datetime import date
from types import SimpleNamespace

from agentscope.state import AgentState

from harbor.agents.dispatch import (
    AgentSubAgentRunner,
    Claim,
    DispatchTaskArgs,
    SubAgentOutput,
    SubAgentRun,
    TaskBrief,
    build_briefs,
    make_task_dispatch_tool,
    read_dispatch_ledger,
    verify_claims,
)
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult


def test_verify_marks_fabricated_product_unverified() -> None:
    output = SubAgentOutput(
        summary="找到一款", claims=[Claim(product_id="P404", statement="很合适")]
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False
    assert verified_ids == ["P1"]


def test_verify_marks_wrong_field_value_unverified() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[Claim(product_id="P1", field="price_major", value=99.0)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_verify_accepts_matching_value_and_int_float_equivalence() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[
            Claim(product_id="P1", field="price_major", value=89),
            Claim(product_id="P1", statement="真实存在"),
        ],
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert [ref.verified for ref in refs] == [True, True]
    assert verified_ids == ["P1"]


def test_verify_marks_unknown_field_unverified() -> None:
    output = SubAgentOutput(
        summary="…",
        claims=[Claim(product_id="P1", field="stock", value=5)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_brief_renders_all_constraints_and_known_ids() -> None:
    brief = TaskBrief(
        task_id="t1",
        goal="母婴宠物类目找 500 元以内的饮水器",
        category="母婴宠物",
        price_max_major=500.0,
        currency="CNY",
        weight_max_kg=1.0,
        excluded_material_tags=("塑料",),
        known_product_ids=("P1",),
    )

    text = brief.render()

    assert "目标：母婴宠物类目找 500 元以内的饮水器" in text
    assert "- category: 母婴宠物" in text
    assert "- price_max_major: 500.0" in text
    assert "- excluded_material_tags: 塑料" in text
    assert "- known_product_ids（用户已见过，仅供参考）: P1" in text


def test_brief_marks_unspecified_fields() -> None:
    text = TaskBrief(task_id="t1", goal="找杯子").render()

    assert "- category: 未指定" in text
    assert "- excluded_material_tags: 无" in text


class _FakeRunner:
    def __init__(
        self, *, delay: float = 0.0, observed: dict | None = None, claims: list | None = None
    ) -> None:
        self.delay = delay
        self.observed = observed or {"P1": {"price_major": 89.0}}
        self.claims = claims or []

    async def run(self, brief):
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=self.claims),
            observed=self.observed,
        )


def _dispatch(tool, state, tasks):
    # ToolChunk → the JSON the model actually reads.
    chunk = asyncio.run(tool.call(_agent_state=state, tasks=tasks))
    return json.loads(chunk.content[0].text)


def test_batch_runs_concurrently_not_serially() -> None:
    tool = make_task_dispatch_tool(None, _FakeRunner(delay=0.4), max_concurrency=3)

    started = time.perf_counter()
    payload = _dispatch(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}])
    elapsed = time.perf_counter() - started

    assert [r["ok"] for r in payload["results"]] == [True, True]
    assert elapsed < 0.65  # serial would be >= 0.8


class _FlakyRunner:
    """Succeeds on the first call, raises on the second."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, brief):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("boom")
        return SubAgentRun(
            output=SubAgentOutput(summary="ok", claims=[]),
            observed={"P1": {"price_major": 89.0}},
        )


def test_one_failing_task_does_not_fail_the_batch() -> None:
    tool = make_task_dispatch_tool(None, _FlakyRunner(), max_concurrency=3)

    payload = _dispatch(tool, AgentState(), [{"goal": "ok"}, {"goal": "fail"}])

    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False


def test_task_and_batch_timeouts_are_soft_failures() -> None:
    task_timeout_tool = make_task_dispatch_tool(
        None,
        _FakeRunner(delay=1.0),
        task_timeout_seconds=0.05,
        batch_timeout_seconds=2.0,
    )
    task = _dispatch(task_timeout_tool, AgentState(), [{"goal": "slow"}])
    assert task["results"][0]["ok"] is False
    assert "超时" in task["results"][0]["summary"]

    batch_timeout_tool = make_task_dispatch_tool(
        None,
        _FakeRunner(delay=1.0),
        task_timeout_seconds=5.0,
        batch_timeout_seconds=0.05,
    )
    batch = _dispatch(batch_timeout_tool, AgentState(), [{"goal": "slow"}])
    assert batch["results"][0]["ok"] is False
    assert "批次超时" in batch["results"][0]["summary"]


def test_batch_timeout_keeps_completed_results() -> None:
    class _StaggeredRunner:
        async def run(self, brief):
            if brief.goal == "slow":
                await asyncio.sleep(1.0)
            return SubAgentRun(
                output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=[]),
                observed={"P1": {"price_major": 89.0}},
            )

    tool = make_task_dispatch_tool(
        None,
        _StaggeredRunner(),
        task_timeout_seconds=5.0,
        batch_timeout_seconds=0.05,
    )

    payload = _dispatch(tool, AgentState(), [{"goal": "fast"}, {"goal": "slow"}])

    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False
    assert "批次超时" in payload["results"][1]["summary"]


def test_too_many_tasks_is_a_soft_failure() -> None:
    tool = make_task_dispatch_tool(None, _FakeRunner(), max_tasks=2)

    payload = _dispatch(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}])

    assert payload["ok"] is False
    assert payload["code"] == "too_many_tasks"


def test_run_writes_a_plan_into_middle_context() -> None:
    state = AgentState()
    tool = make_task_dispatch_tool(None, _FakeRunner())

    _dispatch(tool, state, [{"goal": "找饮水器"}])

    ledger = read_dispatch_ledger(state)
    entry = ledger["batches"]["b1"]["t1"]
    assert entry["goal"] == "找饮水器"
    assert entry["status"] == "ok"
    assert entry["verified_product_ids"] == ["P1"]


def test_two_concurrent_dispatches_keep_both_batches() -> None:
    state = AgentState()
    tool = make_task_dispatch_tool(None, _FakeRunner(delay=0.1))

    async def run():
        return await asyncio.gather(
            tool.call(_agent_state=state, tasks=[{"goal": "a"}]),
            tool.call(_agent_state=state, tasks=[{"goal": "b"}]),
        )

    asyncio.run(run())

    batches = read_dispatch_ledger(state)["batches"]
    assert set(batches) == {"b1", "b2"}
    assert batches["b1"]["t1"]["goal"] == "a"
    assert batches["b2"]["t1"]["goal"] == "b"


def test_build_briefs_assigns_ids_and_merges_blacklist() -> None:
    briefs = build_briefs(
        [DispatchTaskArgs(goal="a", excluded_material_tags=["塑料"]), DispatchTaskArgs(goal="b")],
        blacklist=("玻璃",),
    )

    assert [brief.task_id for brief in briefs] == ["t1", "t2"]
    assert set(briefs[0].excluded_material_tags) == {"塑料", "玻璃"}
    assert set(briefs[1].excluded_material_tags) == {"玻璃"}


def _product(pid: str, *, price: float = 89.0) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=f"商品 {pid}",
        brand="TestBrand",
        category="家居生活",
        tax_category="家居生活",
        description="",
        highlights=[],
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
                price=Money.from_major_units(price, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


class _HitService:
    def __init__(self) -> None:
        self.product = _product("P1", price=89.0)

    def search(self, query, spec, limit=10):
        return SearchResult(
            hits=[SearchHit(product=self.product, score=0.9, selected_sku=self.product.skus[0])],
            total=1,
        )


class _FakeAgent:
    def __init__(self, tool, structured, seen):
        self.tool = tool
        self.structured = structured
        self.state = AgentState()
        self.seen = seen

    async def reply(self, msg, structured_schema=None):
        self.seen.append([tool.name for tool in self.tool])
        await self.tool[0].call(query="饮水器")
        return SimpleNamespace(structured_output=self.structured)


def test_runner_exposes_only_product_search_and_records_hits() -> None:
    seen: list[list[str]] = []
    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _FakeAgent(
            [tool], {"summary": "ok", "claims": []}, seen
        ),
    )
    brief = TaskBrief(task_id="t1", goal="找饮水器", category="家居生活")

    run = asyncio.run(runner.run(brief))

    assert run.output.summary == "ok"
    assert "P1" in run.observed
    assert run.observed["P1"]["price_major"] == 89.0
    assert seen == [["product_search"]]


def test_runner_raises_when_structured_output_is_missing() -> None:
    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _FakeAgent([tool], None, []),
    )

    try:
        asyncio.run(runner.run(TaskBrief(task_id="t1", goal="找饮水器")))
    except RuntimeError as exc:
        assert "结构化输出" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
