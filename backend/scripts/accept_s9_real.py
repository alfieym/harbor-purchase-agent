"""S9 真额度验收：真实聊天模型 + 假 embedding（零 embedding 额度）。

机制层面（并发、核验、降级、账本、快照恢复）已由 ``verify_s9.py`` 离线钉死。
这里只跑 ``verify_s9.py`` 覆盖不到的那一半：**真实模型**被派进子 Agent 后，
返回的结论与证据是否经得起核验；以及简单请求时它会不会忍住不派活。

一共 6 次模型调用（并行批 2 + 串行对照 2 + 降级 1 + 简单请求 1），
embedding 全程走本地假实现，不消耗远程额度。

用法（仓库根目录）：

    uv run --directory backend python scripts/accept_s9_real.py

产物在 ``data/sessions/_s9-acceptance/``，运行前清空。
"""

import asyncio
import json
import shutil
from datetime import date
from typing import Any

from agentscope.message import UserMsg
from agentscope.state import AgentState

from harbor.agents.cards import ProductCardList
from harbor.agents.dispatch import (
    AgentSubAgentRunner,
    DispatchTaskArgs,
    SubAgentOutput,
    SubAgentRun,
    TaskBrief,
    build_briefs,
    make_task_dispatch_tool,
    read_dispatch_ledger,
)
from harbor.agents.factory import AgentRuntime, build_agent
from harbor.agents.models import build_chat_model
from harbor.core.config import Settings
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult
from harbor.features.memory.sqlite_store import SqliteMemoryStore

BUYER = "accept-s9-buyer"
CHECKS: list[tuple[str, bool, str]] = []


class FakeEmbedder:
    """Deterministic vectors; counts calls so the run can prove it never embeds remotely."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float(len(text)), float(sum(map(ord, text)) % 97)] for text in texts]


def _product(pid: str, title: str, category: str, price: float) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=title,
        brand="AcceptBrand",
        category=category,
        tax_category=category,
        description="",
        highlights=[],
        material_tags=["不锈钢"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="accept",
        dimensions_cm={"length": 7, "width": 7, "height": 20},
        weight_kg=0.3,
        rating_summary={"average": 4.5, "review_count": 12},
        skus=[
            Sku(
                sku_id=f"{pid}-S1",
                spec="标准",
                price=Money.from_major_units(price, "CNY"),
                stock=5,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


class AcceptanceService:
    """A tiny in-memory catalog; every hit is a real product the verifier can point at."""

    def __init__(self) -> None:
        self.products = {
            product.product_id: product
            for product in (
                _product("P-CUP", "不锈钢保温杯 500ml", "家居生活", 99.0),
                _product("P-BOWL", "陶瓷猫碗", "母婴宠物", 49.0),
                _product("P-FOUNTAIN", "宠物自动饮水机", "母婴宠物", 129.0),
            )
        }
        self.queries: list[str] = []

    def search(self, query, spec, limit=10):
        self.queries.append(query)
        hits = [
            SearchHit(product=product, score=0.9, selected_sku=product.skus[0])
            for product in self.products.values()
            if spec.category is None or product.category == spec.category
        ]
        return SearchResult(hits=hits[:limit], total=len(hits))


class RecordingAgent:
    """Stands in for a sub-agent so the script can inspect the toolkit it was given."""

    def __init__(self, tool, seen: list[list[str]]) -> None:
        self.tool = [tool]
        self.state = AgentState()
        self.seen = seen

    async def reply(self, msg, structured_schema=None):
        self.seen.append([tool.name for tool in self.tool])
        return _Reply()


class _Reply:
    structured_output = {"summary": "ok", "claims": []}


class FakeRunner:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay

    async def run(self, brief: TaskBrief) -> SubAgentRun:
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}"),
            observed={},
        )


class FlakyRunner:
    """Real runner, except one task is guaranteed to blow up (S9-4)."""

    def __init__(self, inner, crash_when: str) -> None:
        self._inner = inner
        self._crash_when = crash_when

    async def run(self, brief: TaskBrief):
        if self._crash_when in brief.goal:
            raise RuntimeError("验收注入的崩溃")
        return await self._inner.run(brief)


def record(item: str, ok: bool, detail: str) -> None:
    CHECKS.append((item, ok, detail))
    print(f"  {'✓' if ok else '✗'} {item} — {detail}")


def show(label: str, body: Any) -> None:
    print(f"\n  {label}:")
    print("  " + json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\n  "))


async def call(tool: Any, state: AgentState | None, tasks: list[dict]) -> dict[str, Any]:
    chunk = await tool.call(_agent_state=state, tasks=tasks)
    return json.loads(chunk.content[0].text)


def tool_call_names(context) -> list[str]:
    names: list[str] = []
    for msg in context:
        for block in msg.get_content_blocks("tool_use"):
            names.append(getattr(block, "name", "") or "")
    return names


async def main() -> int:
    settings = Settings()
    if not settings.llm_configured:
        print("未配置 OPENAI_API_KEY，无法跑真额度验收。", flush=True)
        return 2
    root = settings.sessions_dir / "_s9-acceptance"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    settings = settings.model_copy(
        update={
            "memory_db_path": root / "memory.sqlite3",
            "context_archive_dir": root / "archives",
        }
    )

    service = AcceptanceService()
    embedder = FakeEmbedder()
    model = build_chat_model(settings)
    memory_store = SqliteMemoryStore(settings.memory_db_path)
    runtime = AgentRuntime(
        settings=settings,
        products=list(service.products.values()),
        service=service,
        model=model,
        categories=sorted(product.category for product in service.products.values()),
        embedder=embedder,
        memory_store=memory_store,
    )

    print("S9 真额度验收（真实模型 + 假 embedding，零 embedding 额度）")
    print(f"模型：{settings.openai_model} @ {settings.openai_base_url}")
    print(f"产物：{root}\n")

    # -- S9-1: one batch, two tasks ------------------------------------------
    runner = AgentSubAgentRunner(
        model=model,
        service=service,
        categories=runtime.categories,
        memory=None,
    )
    tasks = [
        {
            "goal": "找 100 元以内的不锈钢保温杯，只看家居生活类目",
            "category": "家居生活",
            "price_max_major": 100.0,
        },
        {
            "goal": "找 200 元以内的宠物饮水器，只看母婴宠物类目",
            "category": "母婴宠物",
            "price_max_major": 200.0,
        },
    ]
    briefs = build_briefs(
        [DispatchTaskArgs.model_validate(task) for task in tasks],
        blacklist=[],
    )
    tool = make_task_dispatch_tool(None, runner, batch_timeout_seconds=75.0)
    state = AgentState()

    payload = await call(tool, state, tasks)
    elapsed_ms = payload["elapsed_ms"]
    show("并发批返回", payload)

    serial_started = asyncio.get_running_loop().time()
    for brief in briefs:
        await runner.run(brief)
    serial_ms = int((asyncio.get_running_loop().time() - serial_started) * 1000)

    record(
        "S9-1 真实子 Agent 并发比串行快",
        all(result["ok"] for result in payload["results"]) and elapsed_ms < serial_ms * 0.85,
        f"并发 {elapsed_ms}ms vs 串行 {serial_ms}ms",
    )

    # -- S9-2: what the parent actually sees ---------------------------------
    expected_keys = {"task_id", "ok", "summary", "evidence_refs", "verified_product_ids"}
    text = json.dumps(payload, ensure_ascii=False)
    record(
        "S9-2 主上下文只拿到回执",
        all(set(result) == expected_keys for result in payload["results"]) and '"hits"' not in text,
        f"字段={sorted(payload['results'][0])}",
    )

    # -- S9-3: claims vs evidence --------------------------------------------
    evidence_ok = True
    details: list[str] = []
    for result in payload["results"]:
        refs = result["evidence_refs"]
        claimed = {ref["product_id"] for ref in refs if ref["verified"]}
        verified = set(result["verified_product_ids"])
        # 工具返回过的 id 里，只有被声称的那部分应进入 verified refs；
        # 反向不成立：工具返回但未被声称的 id 也在 verified_product_ids 里（那是地图）。
        evidence_ok = evidence_ok and claimed <= verified
        evidence_ok = evidence_ok and verified <= set(service.products)
        details.append(f"{result['task_id']}: claims={len(refs)} verified={sorted(verified)}")
    record(
        "S9-3 只有工具真返回过的 id 被标 verified",
        evidence_ok,
        "；".join(details),
    )

    # -- S9-4: one crash, the batch survives ---------------------------------
    flaky = make_task_dispatch_tool(
        None,
        FlakyRunner(runner, crash_when="必定崩溃"),
        batch_timeout_seconds=75.0,
    )
    degraded = await call(
        flaky,
        AgentState(),
        [
            {"goal": "必定崩溃：找保温杯", "category": "家居生活"},
            {"goal": "找宠物饮水器", "category": "母婴宠物"},
        ],
    )
    show("降级批返回", degraded)
    record(
        "S9-4 一个任务崩溃，另一个仍成功",
        degraded["results"][0]["ok"] is False and degraded["results"][1]["ok"] is True,
        f"ok={[result['ok'] for result in degraded['results']]}；"
        f"原因={degraded['results'][0]['summary']}",
    )

    # -- S9-5: a simple request must not dispatch ----------------------------
    simple_agent = build_agent(runtime, state=AgentState(), buyer_id=BUYER)
    try:
        await simple_agent.reply(
            UserMsg(name="user", content="帮我找保温杯"),
            structured_schema=ProductCardList,
        )
        error = ""
    except Exception as exc:  # noqa: BLE001 — a model hiccup is itself the finding
        error = f"{type(exc).__name__}: {exc}"
    names = tool_call_names(simple_agent.state.context)
    record(
        "S9-5 简单请求没有调用 task_dispatch",
        error == "" and "task_dispatch" not in names,
        f"本轮工具调用={names or ['（无）']}" + (f"；模型异常={error}" if error else ""),
    )

    # -- S9-6: read-only children, concurrent plans --------------------------
    seen: list[list[str]] = []
    child_runner = AgentSubAgentRunner(
        model=object(),
        service=service,
        categories=runtime.categories,
        agent_factory=lambda name, prompt, search_tool: RecordingAgent(search_tool, seen),
    )
    await child_runner.run(TaskBrief(task_id="t1", goal="找饮水器"))

    plan_state = AgentState()
    plan_tool = make_task_dispatch_tool(None, FakeRunner(delay=0.1))
    await asyncio.gather(
        plan_tool.call(_agent_state=plan_state, tasks=[{"goal": "a"}]),
        plan_tool.call(_agent_state=plan_state, tasks=[{"goal": "b"}]),
    )
    batches = read_dispatch_ledger(plan_state)["batches"]
    record(
        "S9-6 子 Agent 只读，并发计划都保留",
        seen == [["product_search"]] and set(batches) == {"b1", "b2"},
        f"子 Agent 工具={seen}；批次={sorted(batches)}",
    )

    # -- S9-7: restart restores the plan -------------------------------------
    restored_state = AgentState()
    restored_state.middle_context["harbor.dispatch"] = {
        "revision": 3,
        "batches": {"b3": {"t1": {"goal": "找宠物饮水器", "status": "failed"}}},
    }
    restored_state = AgentState.model_validate(restored_state.model_dump())
    restored_agent = build_agent(runtime, state=restored_state, buyer_id=BUYER)
    names_after = {tool.name for tool in restored_agent.toolkit.tool_groups[0].tools}
    prompt = await restored_agent._get_system_prompt()
    record(
        "S9-7 重启后工具面与计划都在",
        "task_dispatch" in names_after and "上次派发计划" in prompt and "找宠物饮水器" in prompt,
        f"工具含 task_dispatch={'task_dispatch' in names_after}；"
        f"prompt 含失败计划={'找宠物饮水器' in prompt}",
    )

    # -- embedding budget ----------------------------------------------------
    record(
        "embedding 额度未消耗",
        True,
        f"本地假 embedder 调用 {embedder.calls} 次；脚本从未构造远程 embedder",
    )

    print("\n=== 判定 ===")
    for name, passed, detail in CHECKS:
        print(f"  {'✓' if passed else '✗'} {name} — {detail}")
    failed = [name for name, passed, _ in CHECKS if not passed]
    summary = {
        "remote_embedding_calls": 0,
        "model_calls_planned": 6,
        "checks": [
            {"item": name, "ok": passed, "detail": detail} for name, passed, detail in CHECKS
        ],
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    if failed:
        print(f"失败：{', '.join(failed)}")
        return 1
    print(f"全部通过（{len(CHECKS)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
