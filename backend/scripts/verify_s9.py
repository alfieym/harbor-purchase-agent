"""S9 手工验证：七条验收清单逐条跑给你看（离线，零 API、零 embedding）。

和 ``accept_s9_real.py`` 的分工：

- 这里**不调任何模型、不联网**，子 Agent 用假 runner 代替。它验证的是机制本身：
  批次并发、字段白名单、证据核验、失败降级、只读工具面、并发派发不撞键、状态往返。
- 「模型会不会自己判断该不该派活」由 ``accept_s9_real.py`` 用真实模型覆盖
  （embedding 仍是假向量）。

用法（仓库根目录）：

    uv run --directory backend python scripts/verify_s9.py

产物在 ``data/sessions/_manual-s9/summary.json``（每次运行前清空）。
"""

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agentscope.state import AgentState

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
from harbor.agents.prompts import system_prompt_for
from harbor.core.config import Settings
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult

CHECKS: list[dict[str, Any]] = []


def material(pid: str = "P1", *, price: float = 89.0) -> Product:
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


class HitService:
    """One in-memory hit; no embedding, no network."""

    def __init__(self) -> None:
        self.product = material()

    def search(self, query, spec, limit=10):
        return SearchResult(
            hits=[SearchHit(product=self.product, score=0.9, selected_sku=self.product.skus[0])],
            total=1,
        )


class FakeRunner:
    def __init__(
        self,
        *,
        delay: float = 0.0,
        claims: list[Claim] | None = None,
        fail_on: int | None = None,
    ) -> None:
        self.delay = delay
        self.claims = claims or []
        self.fail_on = fail_on
        self.calls = 0

    async def run(self, brief: TaskBrief) -> SubAgentRun:
        self.calls += 1
        if self.fail_on == self.calls:
            raise RuntimeError("模拟子 Agent 崩溃")
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=self.claims),
            observed={"P1": {"price_major": 89.0}},
        )


class RecordingAgent:
    """Stands in for a real Agent so we can inspect the toolkit it was given."""

    def __init__(self, tool, seen: list[list[str]]) -> None:
        self.tool = [tool]
        self.state = AgentState()
        self.seen = seen

    async def reply(self, msg, structured_schema=None):
        self.seen.append([tool.name for tool in self.tool])
        return _Reply()


@dataclass
class _Reply:
    structured_output: dict[str, Any] = field(
        default_factory=lambda: {"summary": "ok", "claims": []}
    )


def record(item: str, ok: bool, detail: str) -> None:
    CHECKS.append({"item": item, "ok": ok, "detail": detail})
    print(f"  {'✓' if ok else '✗'} {item} — {detail}")


def show(label: str, body: Any) -> None:
    print(f"\n  {label}:")
    print("  " + json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\n  "))


async def call(tool, state: AgentState | None, tasks: list[dict]) -> dict[str, Any]:
    chunk = await tool.call(_agent_state=state, tasks=tasks)
    return json.loads(chunk.content[0].text)


async def section_s9_1() -> None:
    print("\n[1/7] S9-1 一批任务并发，总耗时明显小于串行")
    tool = make_task_dispatch_tool(None, FakeRunner(delay=0.4))

    started = time.perf_counter()
    payload = await call(tool, AgentState(), [{"goal": "找饮水器"}, {"goal": "找猫砂"}])
    elapsed = time.perf_counter() - started

    serial_runner = FakeRunner(delay=0.4)
    serial_started = time.perf_counter()
    await serial_runner.run(TaskBrief(task_id="t1", goal="a"))
    await serial_runner.run(TaskBrief(task_id="t2", goal="b"))
    serial = time.perf_counter() - serial_started
    record(
        "S9-1 并发批",
        all(result["ok"] for result in payload["results"]) and elapsed < serial * 0.75,
        f"并发 {elapsed:.2f}s vs 串行 {serial:.2f}s（每任务 0.4s）",
    )


async def section_s9_2() -> None:
    print("\n[2/7] S9-2 主上下文只看到 summary + evidence_refs + verified_product_ids")
    tool = make_task_dispatch_tool(None, FakeRunner())
    payload = await call(tool, AgentState(), [{"goal": "a"}])
    result = payload["results"][0]
    show("工具返回", payload)
    expected = {"task_id", "ok", "summary", "evidence_refs", "verified_product_ids"}
    text = json.dumps(payload, ensure_ascii=False)
    record(
        "S9-2 字段白名单",
        set(result) == expected and "recall_strategy" not in text and '"hits"' not in text,
        f"字段={sorted(result)}；无原始 hits",
    )


async def section_s9_3() -> None:
    print("\n[3/7] S9-3 声称的商品 id 必须经真实工具返回核验")
    runner = FakeRunner(
        claims=[
            Claim(product_id="P1", field="price_major", value=89.0),
            Claim(product_id="P404", statement="编造的商品"),
            Claim(product_id="P1", field="price_major", value=99.0),
        ]
    )
    tool = make_task_dispatch_tool(None, runner)
    result = (await call(tool, AgentState(), [{"goal": "a"}]))["results"][0]
    refs = result["evidence_refs"]
    show("evidence_refs", refs)
    record(
        "S9-3 编造 id / 错价格标 unverified",
        result["verified_product_ids"] == ["P1"]
        and [ref["verified"] for ref in refs] == [True, False, False],
        f"verified={[ref['verified'] for ref in refs]}；ids={result['verified_product_ids']}",
    )


async def section_s9_4() -> None:
    print("\n[4/7] S9-4 子 Agent 崩溃，整批仍返回")
    tool = make_task_dispatch_tool(None, FakeRunner(fail_on=1))
    payload = await call(tool, AgentState(), [{"goal": "会崩"}, {"goal": "正常"}])
    show("工具返回", payload)
    record(
        "S9-4 单任务失败不拖垮整轮",
        payload["results"][0]["ok"] is False and payload["results"][1]["ok"] is True,
        f"ok={[r['ok'] for r in payload['results']]}",
    )


async def section_s9_5() -> None:
    print("\n[5/7] S9-5 简单请求不该被催着派活")
    prompt = system_prompt_for(["家用电器"], dispatch_enabled=True)
    record(
        "S9-5 三条判据 + 不要派",
        all(phrase in prompt for phrase in ("可并行", "上下文隔离", "链路够深", "任一不成立"))
        and "一次 product_search 能完成的，不要派" in prompt,
        "主 prompt 明确：任一判据不成立就自己做",
    )


async def section_s9_6() -> None:
    print("\n[6/7] S9-6 子 Agent 只读，并发派发不撞键")
    seen: list[list[str]] = []
    runner = AgentSubAgentRunner(
        model=object(),
        service=HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: RecordingAgent(tool, seen),
    )
    await runner.run(TaskBrief(task_id="t1", goal="找饮水器"))

    state = AgentState()
    tool = make_task_dispatch_tool(None, FakeRunner(delay=0.1))
    await asyncio.gather(
        tool.call(_agent_state=state, tasks=[{"goal": "a"}]),
        tool.call(_agent_state=state, tasks=[{"goal": "b"}]),
    )
    batches = read_dispatch_ledger(state)["batches"]
    show("计划账本", batches)
    record(
        "S9-6 只读工具面 + 两个批次都保留",
        seen == [["product_search"]] and set(batches) == {"b1", "b2"},
        f"子 Agent 工具={seen}；批次={sorted(batches)}",
    )


async def section_s9_7() -> None:
    print("\n[7/7] S9-7 快照往返后计划可读")
    state = AgentState()
    tool = make_task_dispatch_tool(None, FakeRunner())
    await call(tool, state, [{"goal": "找饮水器"}])

    restored = AgentState.model_validate(state.model_dump())
    ledger = read_dispatch_ledger(restored)
    entry = ledger.get("batches", {}).get("b1", {}).get("t1", {})
    show("恢复后的计划", ledger)
    record(
        "S9-7 AgentState 往返保留计划",
        restored.middle_context.get(DISPATCH_STATE_KEY) is not None
        and entry.get("goal") == "找饮水器"
        and entry.get("status") == "ok",
        f"revision={ledger.get('revision')}；"
        f"t1 goal={entry.get('goal')} status={entry.get('status')}",
    )


async def main() -> int:
    root = Settings().sessions_dir / "_manual-s9"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    print("S9 手工验证（离线：假 runner / 假 embedding，零 API）")
    print(f"产物目录：{root}")

    await section_s9_1()
    await section_s9_2()
    await section_s9_3()
    await section_s9_4()
    await section_s9_5()
    await section_s9_6()
    await section_s9_7()

    failed = [check["item"] for check in CHECKS if not check["ok"]]
    summary = {
        "remote_embedding_calls": 0,
        "remote_model_calls": 0,
        "checks": CHECKS,
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n逐条证据已写入：{root / 'summary.json'}")
    if failed:
        print(f"\n失败：{', '.join(failed)}")
        return 1
    print(f"\n全部通过（{len(CHECKS)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
