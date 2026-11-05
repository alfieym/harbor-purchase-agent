"""S8 手工验证：八条验收清单逐条跑给你看（离线，零 API、零 embedding）。

和 ``accept_s8_real.py`` 的分工：

- 这里**不调任何模型、不联网**，作用域由脚本显式指定。它验证的是机制本身：
  暂存队列、确认落库、去重、409、200 上限、代码级材质过滤。
- 「模型会不会自己选对作用域、会不会先问用户」由 ``accept_s8_real.py`` 用真实
  ``qwen-plus`` 覆盖（embedding 仍是假向量）。

跑的是真实 SQLite 文件、真实 ``AgentState``、真实 ``FunctionTool.call``：
工具结果就是模型会看到的那段 JSON，注入块就是每轮末尾那条 ``UserMsg``。

用法（仓库根目录）：

    uv run --directory backend python scripts/verify_s8.py

产物在 ``data/sessions/_manual-s8/``（每次运行前清空）：

- ``memory.sqlite3``：可以直接用 sqlite3 或 DB Browser 打开逐行检查
- ``summary.json``：每条清单的 ✓/✗ 与证据，方便贴进验收记录
"""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.state import AgentState

from harbor.agents.context_governance import ContextGovernanceMiddleware, SessionArchiveOffloader
from harbor.agents.preferences import PreferenceMemory, make_preference_tools
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.memory.sqlite_store import SqliteMemoryStore

BUYER = "manual-s8-buyer"
CAP_BUYER = "manual-s8-cap-buyer"
OFFICIAL = "我以后只看官方店"
UPDATED = "我以后只看官方旗舰店"

CHECKS: list[dict[str, Any]] = []


class FakeEmbedder:
    """Deterministic vectors; no network. The call count is part of the evidence."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text)), float(sum(map(ord, text)) % 97)] for text in texts]


class RecordingService:
    """Captures the ``ProductSearchSpec`` the tool hands to the catalog service."""

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def search(self, query: str, spec: Any, limit: int = 10) -> SearchResult:
        self.specs.append(spec)
        return SearchResult(hits=[], total=0)


def record(item: str, ok: bool, detail: str) -> None:
    CHECKS.append({"item": item, "ok": ok, "detail": detail})
    print(f"  {'✓' if ok else '✗'} {item} — {detail}")


def show(label: str, body: Any) -> None:
    print(f"\n  {label}:")
    print("  " + json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\n  "))


async def call(tool: Any, state: AgentState, **kwargs: Any) -> dict[str, Any]:
    chunk = await tool.call(_agent_state=state, **kwargs)
    return json.loads(chunk.content[0].text)


def injected(governance: ContextGovernanceMiddleware, state: AgentState) -> str:
    """The exact trailing UserMsg the model would receive.

    Private API on purpose: this script verifies the real injection string, not
    a re-implementation of it.
    """
    messages = governance._with_preferences([], SimpleNamespace(state=state))
    if not messages:
        return ""
    return messages[-1].get_text_content() or ""


def new_memory(store: SqliteMemoryStore, embedder: FakeEmbedder, buyer: str = BUYER):
    return PreferenceMemory(
        buyer_id=buyer,
        store=store,
        embedder=embedder,
        vector_model="fake-embedding-v1",
    )


async def section_s8_6(
    memory: PreferenceMemory,
    store: SqliteMemoryStore,
    state: AgentState,
    tools: dict[str, Any],
    governance: ContextGovernanceMiddleware,
) -> str:
    print("\n[1/8] S8-6 未确认时不落库；下一轮提示「还有待确认的记忆操作」")
    staged = await call(tools["add_preference"], state, text=OFFICIAL, scope="long_term")
    show("add_preference 返回", staged)

    pending = memory.pending_items(state)
    rows = await store.list(buyer_id=BUYER, kind=None)
    record(
        "S8-6 只暂存，不落库",
        staged.get("pending") is True and len(pending) == 1 and rows == [],
        f"pending={[item.text for item in pending]}；rows={[r.text for r in rows]}",
    )

    # A new turn must not drop the unanswered operation.
    await memory.begin_turn(state)
    text = injected(governance, state)
    print("\n  下一轮注入：")
    print("  " + text.replace("\n", "\n  "))
    record(
        "S8-6 下一轮仍提示",
        "还有待确认的记忆操作" in text and len(memory.pending_items(state)) == 1,
        "注入块包含「还有待确认的记忆操作」",
    )
    return pending[0].id


async def section_s8_1(
    memory: PreferenceMemory,
    store: SqliteMemoryStore,
    state: AgentState,
    tools: dict[str, Any],
    embedder: FakeEmbedder,
    archive_root: Path,
    operation_id: str,
) -> None:
    print("\n[2/8] S8-1 确认后写入；新开会话仍然生效")
    confirmed = await call(tools["confirm_preference"], state, operation_id=operation_id)
    show("confirm_preference 返回", confirmed)

    rows = await store.list(buyer_id=BUYER, kind=None)
    fresh = new_memory(store, embedder)
    fresh_state = AgentState()
    await fresh.begin_turn(fresh_state)
    fresh_governance = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(archive_root / "archives-fresh"),
        preferences=fresh,
    )
    text = injected(fresh_governance, fresh_state)
    long_line = next((line for line in text.splitlines() if line.startswith("[长期]")), "无")

    record(
        "S8-1 确认后写入并提供新会话可见性",
        len(rows) == 1
        and rows[0].text == OFFICIAL
        and memory.pending_items(state) == []
        and "[长期]" in text
        and OFFICIAL in text,
        f"rows={[r.text for r in rows]}；新会话注入={long_line}",
    )


async def section_s8_3(
    memory: PreferenceMemory, store: SqliteMemoryStore, state: AgentState, tools: dict[str, Any]
) -> None:
    print("\n[3/8] S8-3 同一句话写两次只留一条（去重）")
    again = await call(tools["add_preference"], state, text=OFFICIAL, scope="long_term")
    duplicate = await call(
        tools["confirm_preference"], state, operation_id=again["operation"]["id"]
    )
    show("第二次 confirm 返回", duplicate)

    rows = await store.list(buyer_id=BUYER, kind=None)
    record(
        "S8-3 去重",
        duplicate.get("duplicate") is True and len(rows) == 1,
        f"duplicate={duplicate.get('duplicate')}；count={len(rows)}",
    )


async def section_s8_4(store: SqliteMemoryStore, state: AgentState, tools: dict[str, Any]) -> None:
    print("\n[4/8] S8-4 用旧 version 更新 → 409，不覆盖新值")
    [target] = await store.list(buyer_id=BUYER, kind="preference")
    first = await call(
        tools["update_preference"],
        state,
        preference_id=target.id,
        text=UPDATED,
        expected_version=target.version,
    )
    show("第一次 update 返回", first)

    stale = await call(
        tools["update_preference"],
        state,
        preference_id=target.id,
        text="我以后只看官方直营店",
        expected_version=target.version,
    )
    show("拿旧 version 再 update", stale)

    current = await store.get(buyer_id=BUYER, memory_id=target.id)
    record(
        "S8-4 旧 version 409 且不覆盖",
        stale.get("code") == "version_conflict"
        and stale.get("status") == 409
        and current is not None
        and current.text == UPDATED,
        f"status={stale.get('status')}；current={current.text if current else None}",
    )


async def section_s8_5(store: SqliteMemoryStore, embedder: FakeEmbedder) -> None:
    print("\n[5/8] S8-5 第 201 条被拒，dislike 不受上限影响")
    # A separate buyer keeps the other sections' state readable.
    cap = new_memory(store, embedder, buyer=CAP_BUYER)
    cap_state = AgentState()
    await cap.begin_turn(cap_state)
    cap_tools = {tool.name: tool for tool in make_preference_tools(cap)}

    for index in range(200):
        await cap.add(cap_state, "long_term", f"偏好 {index}")
    overflow = await call(
        cap_tools["add_preference"], cap_state, text="偏好 200", scope="long_term"
    )
    rejected = await call(
        cap_tools["confirm_preference"], cap_state, operation_id=overflow["operation"]["id"]
    )
    show("第 201 条 confirm 返回", rejected)

    dislike = await cap.add(
        cap_state, "long_term", "不要玻璃", kind="dislike", material_tags=("玻璃",)
    )
    record(
        "S8-5 上限拒绝且 dislike 豁免",
        rejected.get("code") == "limit_reached"
        and rejected.get("limit") == 200
        and await store.count(buyer_id=CAP_BUYER) == 200
        and dislike.preference.id.startswith("pref_"),
        f"code={rejected.get('code')}；偏好数={await store.count(buyer_id=CAP_BUYER)}；"
        f"dislike={dislike.preference.id}",
    )


async def section_s8_7(
    memory: PreferenceMemory, store: SqliteMemoryStore, state: AgentState, tools: dict[str, Any]
) -> None:
    print("\n[6/8] S8-7「这次预算 2000」不进入长期记忆")
    await call(tools["add_preference"], state, text="这次预算 2000 以内", scope="turn")
    turn_items = memory.turn_items()
    leaked = [row for row in await store.list(buyer_id=BUYER, kind=None) if "2000" in row.text]
    record(
        "S8-7 作用域正确",
        any("2000" in item.text for item in turn_items) and leaked == [],
        f"turn={[item.text for item in turn_items]}；长期 2000={[r.text for r in leaked]}",
    )


async def section_s8_2(
    memory: PreferenceMemory, store: SqliteMemoryStore, state: AgentState, tools: dict[str, Any]
) -> None:
    print("\n[7/8] S8-2 撤回 dislike 后不再过滤塑料")
    staged = await call(
        tools["add_preference"],
        state,
        text="不要塑料的杯子",
        scope="long_term",
        kind="dislike",
        material_tags=["塑料"],
    )
    confirmed = await call(
        tools["confirm_preference"], state, operation_id=staged["operation"]["id"]
    )
    memory_id = confirmed["preference"]["id"]
    before = memory.blacklisted_material_tags(state)

    deleted = await call(tools["delete_preference"], state, preference_id=memory_id)
    after = memory.blacklisted_material_tags(state)
    show("删除返回", deleted)

    record(
        "S8-2 撤回后黑名单清空",
        "塑料" in before
        and "塑料" not in after
        and await store.get(buyer_id=BUYER, memory_id=memory_id) is None,
        f"before={before}；after={after}",
    )


async def section_s8_8(memory: PreferenceMemory, state: AgentState, tools: dict[str, Any]) -> None:
    print("\n[8/8] S8-8 材质黑名单由代码合并进检索条件")
    staged = await call(
        tools["add_preference"],
        state,
        text="不要玻璃的",
        scope="long_term",
        kind="dislike",
        material_tags=["玻璃"],
    )
    await call(tools["confirm_preference"], state, operation_id=staged["operation"]["id"])

    service = RecordingService()
    search = make_product_search_tool(service, memory=memory)
    await search.call(_agent_state=state, query="保温杯", excluded_material_tags=[])
    # The model passes its own exclusion; the remembered dislike must be added to it.
    await search.call(_agent_state=state, query="保温杯", excluded_material_tags=["塑料"])

    specs = [list(spec.excluded_material_tags) for spec in service.specs]
    print(f"\n  两次 product_search 收到的 excluded_material_tags：{specs}")
    record(
        "S8-8 代码级硬过滤",
        len(specs) == 2 and specs[0] == ["玻璃"] and set(specs[1]) == {"塑料", "玻璃"},
        f"specs={specs}（模型第二次只传了塑料）",
    )


async def main() -> int:
    settings = Settings()
    root = settings.context_archive_dir.parent / "_manual-s8"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    store = SqliteMemoryStore(root / "memory.sqlite3")
    embedder = FakeEmbedder()
    memory = new_memory(store, embedder)
    state = AgentState()
    await memory.begin_turn(state)
    tools = {tool.name: tool for tool in make_preference_tools(memory)}
    governance = ContextGovernanceMiddleware(
        offloader=SessionArchiveOffloader(root / "archives"), preferences=memory
    )

    print("S8 手工验证（真实 SQLite + 真实工具，零 API、零 embedding）")
    print(f"长期库：{root / 'memory.sqlite3'}")
    print(f"买家：{BUYER}（上限场景用独立的 {CAP_BUYER}）")

    operation_id = await section_s8_6(memory, store, state, tools, governance)
    await section_s8_1(memory, store, state, tools, embedder, root, operation_id)
    await section_s8_3(memory, store, state, tools)
    await section_s8_4(store, state, tools)
    await section_s8_5(store, embedder)
    await section_s8_7(memory, store, state, tools)
    await section_s8_2(memory, store, state, tools)
    await section_s8_8(memory, state, tools)

    rows = await store.list(buyer_id=BUYER, kind=None, limit=None)
    print("\n=== 最终长期记录（main buyer）===")
    for row in rows:
        print(
            f"  {row.id}  {row.kind:<10} v{row.version}  {row.text}  tags={list(row.material_tags)}"
        )

    failed = [check["item"] for check in CHECKS if not check["ok"]]
    summary = {
        "buyer": BUYER,
        "cap_buyer": CAP_BUYER,
        "embedding_calls": embedder.calls,
        "remote_embedding_calls": 0,
        "checks": CHECKS,
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n本地假 embedding：{embedder.calls} 次（无远程调用）")
    print(f"逐条证据已写入：{root / 'summary.json'}")
    print("\n自己再查一遍 SQLite：")
    print(
        f'  sqlite3 "{root / "memory.sqlite3"}" '
        '"select id, kind, version, material_tags, text from memories order by created_at;"'
    )
    if failed:
        print(f"\n失败：{', '.join(failed)}")
        return 1
    print(f"\n全部通过（{len(CHECKS)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
