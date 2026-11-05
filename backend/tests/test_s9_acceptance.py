"""S9 acceptance checklist: one test per item, against the tool surface."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from agentscope.state import AgentState
from qdrant_client import QdrantClient

from harbor.agents.dispatch import (
    DISPATCH_STATE_KEY,
    AgentSubAgentRunner,
    Claim,
    SubAgentOutput,
    SubAgentRun,
    TaskBrief,
    make_task_dispatch_tool,
    read_dispatch_ledger,
)
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"


# --- test helpers (copied from test_dispatch.py; not production code) -------


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


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


class _Runner:
    def __init__(self, *, delay=0.0, fail_at=None, claims=None, observed=None):
        self.delay = delay
        self.fail_at = fail_at
        self.calls = 0
        self.claims = claims or []
        self.observed = observed or {"P1": {"price_major": 89.0}}

    async def run(self, brief):
        self.calls += 1
        if self.fail_at == self.calls:
            raise RuntimeError("boom")
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=self.claims),
            observed=self.observed,
        )


def _run(tool, state, tasks):
    chunk = asyncio.run(tool.call(_agent_state=state, tasks=tasks))
    return json.loads(chunk.content[0].text)


# --- S9-1 .. S9-7 -----------------------------------------------------------


def test_s9_1_batch_is_faster_than_serial() -> None:
    tool = make_task_dispatch_tool(None, _Runner(delay=0.4), max_concurrency=3)

    started = time.perf_counter()
    payload = _run(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}])
    elapsed = time.perf_counter() - started

    assert all(result["ok"] for result in payload["results"])
    assert elapsed < 0.65  # serial: 2 × 0.4 = 0.8


def test_s9_2_parent_sees_only_summary_and_evidence() -> None:
    tool = make_task_dispatch_tool(None, _Runner())
    payload = _run(tool, AgentState(), [{"goal": "a"}])

    result = payload["results"][0]
    assert set(result) == {
        "task_id",
        "ok",
        "summary",
        "evidence_refs",
        "verified_product_ids",
    }
    text = json.dumps(payload, ensure_ascii=False)
    assert "recall_strategy" not in text
    assert '"hits"' not in text


def test_s9_3_claims_are_verified_against_real_tool_results() -> None:
    runner = _Runner(
        claims=[
            Claim(product_id="P1", field="price_major", value=89.0),
            Claim(product_id="P404", statement="不存在"),
            Claim(product_id="P1", field="price_major", value=99.0),
        ]
    )
    tool = make_task_dispatch_tool(None, runner)

    result = _run(tool, AgentState(), [{"goal": "a"}])["results"][0]

    assert result["verified_product_ids"] == ["P1"]
    assert [ref["verified"] for ref in result["evidence_refs"]] == [True, False, False]


def test_s9_4_one_failure_does_not_fail_the_turn() -> None:
    tool = make_task_dispatch_tool(None, _Runner(fail_at=1))

    payload = _run(tool, AgentState(), [{"goal": "fail"}, {"goal": "ok"}])

    assert payload["results"][0]["ok"] is False
    assert payload["results"][1]["ok"] is True


def test_s9_5_simple_request_gets_no_dispatch_guidance_pressure() -> None:
    from harbor.agents.prompts import system_prompt_for

    prompt = system_prompt_for(["家用电器"], dispatch_enabled=True)

    assert "一次 product_search 能完成的，不要派" in prompt


def test_s9_6_subagents_are_read_only_and_plans_do_not_collide() -> None:
    seen: list[list[str]] = []

    class _Agent:
        def __init__(self, tool):
            self.tool = [tool]
            self.state = AgentState()

        async def reply(self, msg, structured_schema=None):
            seen.append([tool.name for tool in self.tool])
            return type("Reply", (), {"structured_output": {"summary": "ok", "claims": []}})()

    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _Agent(tool),
    )
    asyncio.run(runner.run(TaskBrief(task_id="t1", goal="找饮水器")))
    assert seen == [["product_search"]]

    state = AgentState()
    tool = make_task_dispatch_tool(None, _Runner(delay=0.1))

    async def dispatch_twice():
        # ``gather`` must be built inside the loop; outside it Python 3.11
        # raises "There is no current event loop".
        return await asyncio.gather(
            tool.call(_agent_state=state, tasks=[{"goal": "a"}]),
            tool.call(_agent_state=state, tasks=[{"goal": "b"}]),
        )

    asyncio.run(dispatch_twice())
    assert set(read_dispatch_ledger(state)["batches"]) == {"b1", "b2"}


def test_s9_7_restart_restores_tools_and_plan(monkeypatch, tmp_path: Path) -> None:
    import harbor.agents.factory as factory
    from harbor.agents.factory import build_agent, build_runtime
    from harbor.core.config import Settings

    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        memory_db_path=tmp_path / "memory.sqlite3",
    )
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _s: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _m: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_a, **_k: (QdrantClient(":memory:"), "fp-123")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    runtime = build_runtime(settings, data_path=FIXTURE)

    state = AgentState()
    state.middle_context[DISPATCH_STATE_KEY] = {
        "revision": 2,
        "batches": {"b2": {"t1": {"goal": "找饮水器", "status": "failed"}}},
    }
    restored = AgentState.model_validate(state.model_dump())

    agent = build_agent(runtime, state=restored)

    names = {tool.name for tool in agent.toolkit.tool_groups[0].tools}
    assert "task_dispatch" in names
    prompt = asyncio.run(agent._get_system_prompt())
    assert "上次派发计划" in prompt and "找饮水器" in prompt
