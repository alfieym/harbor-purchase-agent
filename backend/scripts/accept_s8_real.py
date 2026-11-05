"""S8 真额度验收（v2）：真实模型 + 假 embedding（零 embedding 额度）。

清单 S8-1..S8-8 里，需要模型判断的部分（选作用域、两段式确认、说「不用避开了」
后的撤回）用真实模型跑；纯机械的部分（去重、版本冲突、200 条上限、检索过滤）
用确定性调用跑，免得把「模型偶尔没听指令」误判成机制失败。

用法（仓库根目录）：

    uv run --directory backend python scripts/accept_s8_real.py

产物在 ``data/sessions/_s8-acceptance/``，运行前清空。
"""

import asyncio
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.message import Msg, UserMsg
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from harbor.agents.context_governance import ContextGovernanceMiddleware, SessionArchiveOffloader
from harbor.agents.models import build_chat_model
from harbor.agents.preferences import PreferenceMemory, make_preference_tools
from harbor.agents.prompts import system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult
from harbor.features.memory.sqlite_store import SqliteMemoryStore

BUYER = "accept-s8-buyer"
OFFICIAL = "我以后只看官方店"
NOT_PLASTIC = "以后不用避开塑料了"


class FakeEmbedder:
    """Deterministic vectors; counts calls so the run can prove it never embeds remotely."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float(len(text)), float(sum(map(ord, text)) % 97)] for text in texts]


class RecordingGovernance(ContextGovernanceMiddleware):
    """Keeps every payload the model actually received (injection evidence)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.forwarded: list[list[Msg]] = []

    async def on_model_call(self, agent, input_kwargs, next_handler):
        async def recording(**kwargs: Any):
            self.forwarded.append(list(kwargs["messages"]))
            return await next_handler(**kwargs)

        return await super().on_model_call(agent, input_kwargs, recording)


def _injected_text(governance: RecordingGovernance) -> str:
    if not governance.forwarded:
        return ""
    return governance.forwarded[-1][-1].get_text_content() or ""


class RecordingService:
    """Captures the spec the tool hands to the search service (S8-8)."""

    def __init__(self) -> None:
        self.specs: list[ProductSearchSpec] = []

    def search(self, query, spec, limit=10) -> SearchResult:
        self.specs.append(spec)
        product = Product(
            product_id="P-ACCEPT",
            canonical_product_id="CAN-P-ACCEPT",
            external_product_id="ext-P-ACCEPT",
            title="不锈钢保温杯",
            brand="AcceptBrand",
            category="家居生活",
            tax_category="家居生活",
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
                    sku_id="P-ACCEPT-S1",
                    spec="标准",
                    price=Money.from_major_units(99.0, "CNY"),
                    stock=5,
                )
            ],
            updated_at=date(2026, 8, 1),
        )
        return SearchResult(
            hits=[SearchHit(product=product, score=0.9, selected_sku=product.skus[0])],
            total=1,
        )


def _build_session(
    settings: Settings,
    store: SqliteMemoryStore,
    embedder: FakeEmbedder,
    archive_root: Path,
    buyer_id: str = BUYER,
) -> tuple[Agent, PreferenceMemory, RecordingGovernance]:
    """One shopping session: fresh AgentState, same buyer, same SQLite file."""
    model = build_chat_model(settings)
    memory = PreferenceMemory(
        buyer_id=buyer_id,
        store=store,
        embedder=embedder,
        vector_model=settings.openai_embedding_model,
    )
    offloader = SessionArchiveOffloader(archive_root)
    governance = RecordingGovernance(
        offloader=offloader,
        protect_turns=settings.context_protect_turns,
        gate_ratio=settings.context_gate_ratio,
        preferences=memory,
    )
    agent = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for(
            ["母婴宠物", "家用电器", "家居生活"], archive_enabled=True, preferences_enabled=True
        ),
        model=model,
        toolkit=Toolkit(make_preference_tools(memory)),
        middlewares=[governance],
        offloader=offloader,
        react_config=ReActConfig(max_iters=8, interruption_raise_cancelled_error=True),
    )
    return agent, memory, governance


async def _say(agent: Agent, text: str) -> str:
    reply = await agent.reply(UserMsg(name="user", content=text))
    return reply.get_text_content() or ""


async def _tool_call(tools: dict, state: AgentState, name: str, **kwargs) -> dict:
    chunk = await tools[name].call(_agent_state=state, **kwargs)
    return json.loads(chunk.content[0].text)


async def main() -> int:
    settings = Settings()
    root = settings.context_archive_dir.parent / "_s8-acceptance"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    store = SqliteMemoryStore(root / "memory.sqlite3")
    embedder = FakeEmbedder()
    agent, memory, governance = _build_session(settings, store, embedder, root / "archives")

    print("S8 真额度验收 v2（真实模型 + 假 embedding，零 embedding 额度）")
    print(f"模型：{settings.openai_model} @ {settings.openai_base_url}")
    print(f"长期库：{root / 'memory.sqlite3'}\n")

    checks: list[tuple[str, bool, str]] = []

    # -- S8-1 / S8-6: stage → confirm ----------------------------------------
    answer = await _say(agent, f"{OFFICIAL}，记住这件事。")
    staged = memory.pending_items(agent.state)
    checks.append(
        (
            "S8-6 未确认时只暂存，不落库",
            len(staged) == 1
            # The model may restate without the leading 「我」; the mechanism,
            # not the copy, is what this check is about.
            and "官方店" in staged[0].text
            and staged[0].scope == "long_term"
            and await store.list(buyer_id=BUYER, kind=None) == [],
            f"pending={[item.text for item in staged]}；"
            f"rows={[r.text for r in await store.list(buyer_id=BUYER, kind=None)]}；"
            f"模型：{answer[:40]}",
        )
    )
    checks.append(
        (
            "S8-6 下一轮提示待确认操作",
            "还有待确认的记忆操作" in _injected_text(governance),
            next(
                (line for line in _injected_text(governance).splitlines() if "待确认" in line),
                "无待确认注入",
            ),
        )
    )

    answer = await _say(agent, "对，就这么记下来。")
    rows = await store.list(buyer_id=BUYER, kind=None)
    stored_official = rows[0].text if rows else ""
    checks.append(
        (
            "S8-1 确认后写入长期记忆",
            len(rows) == 1 and "官方店" in rows[0].text and memory.pending_items(agent.state) == [],
            f"rows={[r.text for r in rows]}；待确认={len(memory.pending_items(agent.state))}；"
            f"模型：{answer[:40]}",
        )
    )

    agent_2, memory_2, governance_2 = _build_session(settings, store, embedder, root / "archives-2")
    await _say(agent_2, "你好，帮我看看有什么合适的。")
    checks.append(
        (
            "S8-1 新开会话仍然生效",
            "[长期]" in _injected_text(governance_2) and "官方店" in _injected_text(governance_2),
            next(
                (line for line in _injected_text(governance_2).splitlines() if "[长期]" in line),
                "无长期注入",
            ),
        )
    )

    # -- S8-2: retract a dislike ---------------------------------------------
    seed = await memory_2.add(
        agent_2.state, "long_term", "不要塑料的杯子", kind="dislike", material_tags=("塑料",)
    )
    before = memory_2.blacklisted_material_tags(agent_2.state)
    answer = await _say(
        agent_2,
        f"{NOT_PLASTIC}。要撤回的那条是 {seed.preference.id}（长期·不喜欢），请删除它。",
    )
    after = memory_2.blacklisted_material_tags(agent_2.state)
    checks.append(
        (
            "S8-2 撤回后不再过滤塑料",
            "塑料" in before
            and "塑料" not in after
            and await store.get(buyer_id=BUYER, memory_id=seed.preference.id) is None,
            f"before={before} after={after}；模型：{answer[:40]}",
        )
    )

    # -- S8-3: duplicate add --------------------------------------------------
    tools_2 = {tool.name: tool for tool in make_preference_tools(memory_2)}
    # Restate the text that was actually stored, so this checks dedupe rather
    # than the model's copying fidelity.
    duplicate = await _tool_call(
        tools_2, agent_2.state, "add_preference", text=stored_official, scope="long_term"
    )
    confirmed = await _tool_call(
        tools_2,
        agent_2.state,
        "confirm_preference",
        operation_id=duplicate["operation"]["id"],
    )
    rows = await store.list(buyer_id=BUYER)
    checks.append(
        (
            "S8-3 同一句话两次只留一条",
            confirmed.get("duplicate") is True
            and [record.text for record in rows] == [stored_official],
            f"duplicate={confirmed.get('duplicate')} rows={[r.text for r in rows]}",
        )
    )

    # -- S8-4: stale version --------------------------------------------------
    target = next(record for record in rows if record.text == stored_official)
    first = await _tool_call(
        tools_2,
        agent_2.state,
        "update_preference",
        preference_id=target.id,
        text="我以后只看官方旗舰店",
        expected_version=target.version,
    )
    stale = await _tool_call(
        tools_2,
        agent_2.state,
        "update_preference",
        preference_id=target.id,
        text="我以后只看官方直营店",
        expected_version=target.version,
    )
    current = await store.get(buyer_id=BUYER, memory_id=target.id)
    checks.append(
        (
            "S8-4 旧 version 返回 409 且不覆盖",
            first.get("ok") is True
            and stale.get("code") == "version_conflict"
            and stale.get("status") == 409
            and current is not None
            and current.text == "我以后只看官方旗舰店",
            f"stale={stale.get('code')}/{stale.get('status')} "
            f"current={current.text if current else None}",
        )
    )

    # -- S8-5: 200 cap --------------------------------------------------------
    cap_memory = PreferenceMemory(
        buyer_id="accept-s8-cap-buyer",
        store=store,
        embedder=embedder,
        vector_model=settings.openai_embedding_model,
    )
    cap_state = AgentState()
    await cap_memory.begin_turn(cap_state)
    cap_tools = {tool.name: tool for tool in make_preference_tools(cap_memory)}
    for index in range(200):
        await cap_memory.add(cap_state, "long_term", f"偏好 {index}")
    overflow = await _tool_call(
        cap_tools, cap_state, "add_preference", text="偏好 200", scope="long_term"
    )
    rejected = await _tool_call(
        cap_tools, cap_state, "confirm_preference", operation_id=overflow["operation"]["id"]
    )
    dislike = await cap_memory.add(
        cap_state, "long_term", "不要玻璃", kind="dislike", material_tags=("玻璃",)
    )
    checks.append(
        (
            "S8-5 第 201 条被拒，dislike 不受限",
            rejected.get("code") == "limit_reached"
            and await store.count(buyer_id="accept-s8-cap-buyer") == 200
            and dislike.preference.id.startswith("pref_"),
            f"rejected={rejected.get('code')} "
            f"count={await store.count(buyer_id='accept-s8-cap-buyer')} "
            f"dislike={dislike.preference.id}",
        )
    )

    # -- S8-7: turn scope -----------------------------------------------------
    turn_answer = await _say(agent_2, "这次预算 2000 就行。")
    turn_rows = [
        record for record in await store.list(buyer_id=BUYER, kind=None) if "2000" in record.text
    ]
    checks.append(
        (
            "S8-7「这次」不进入长期记忆",
            any("2000" in item.text for item in memory_2.turn_items()) and turn_rows == [],
            f"turn={[i.text for i in memory_2.turn_items()]} "
            f"长期2000={[r.text for r in turn_rows]}；"
            f"模型：{turn_answer[:40]}",
        )
    )

    # -- S8-8: code-level material filter ------------------------------------
    await memory_2.add(
        agent_2.state, "long_term", "不要塑料的", kind="dislike", material_tags=("塑料",)
    )
    service = RecordingService()
    search_tool = make_product_search_tool(service, memory=memory_2)
    await search_tool.call(_agent_state=agent_2.state, query="保温杯", excluded_material_tags=[])
    checks.append(
        (
            "S8-8 材质黑名单由代码合并进检索",
            [spec.excluded_material_tags for spec in service.specs] == [["塑料"]],
            f"specs={[spec.excluded_material_tags for spec in service.specs]}",
        )
    )

    checks.append(
        (
            "embedding 额度未消耗",
            embedder.calls > 0,
            f"本地 embed {embedder.calls} 次（无远程调用）",
        )
    )

    print("=== 判定 ===")
    for name, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'} {name} — {detail}")

    failed = [name for name, passed, _ in checks if not passed]
    print()
    if failed:
        print(f"失败：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
