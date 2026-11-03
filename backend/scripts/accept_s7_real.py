"""S7 真额度验收：用真实模型跑压缩、摘要、回查与偏好注入。

与 ``verify_s7.py`` 的区别：那边摘要模型是桩，零 API；这里**每次模型调用都是真的**
（DashScope qwen-plus），检索也走**真实 S3 链路**——真实 embedder、真实 Qdrant 索引、
真实 rerank。查询只花一次 embedding；索引复用磁盘上已建好的那份（目录指纹已校验），
不会重新 embedding 3700 条商品。

流程（``context_size`` 缩到 8000，触发比例保持 0.6，闸门 0.85）：

1. 预置一段很长的历史（超过闸门），真实用户轮触发**第一次真实压缩**；
2. 模型真实调用 ``product_search``（真实检索）并产出商品卡；
3. 再灌长历史把商品卡挤出保护边界，真实用户轮触发**第二次真实压缩**；
4. 同一轮问「我预算多少？刚才推荐的 product_id 是哪个？」——模型只能从摘要/附录回答。

用法（仓库根目录，``backend`` 下执行）：

    uv run --directory backend python scripts/accept_s7_real.py

产物在 ``data/sessions/archives/_real-s7/``，运行前清空。
"""

import asyncio
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg
from agentscope.tool import Toolkit
from qdrant_client import QdrantClient

from harbor.agents.cards import ProductCardList
from harbor.agents.context_governance import (
    ContextGovernanceMiddleware,
    SessionArchiveOffloader,
    make_read_archive_tool,
)
from harbor.agents.factory import DEFAULT_DATA_PATH, INDEX_DIR
from harbor.agents.models import build_chat_model
from harbor.agents.permissions import allow_product_search_tool
from harbor.agents.prompts import SUMMARY_TEMPLATE, system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
from harbor.features.catalog.models import Product
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.embedder import OpenAIEmbedder
from harbor.features.catalog.search.rerank import FastembedReranker
from harbor.features.catalog.search.service import CatalogSearchService

# The checklist's ratios are kept; only the window is scaled down so a handful of
# real turns is enough to cross the trigger (the real window is 131072).
CONTEXT_SIZE = 8000
CARD_PRODUCTS = ("P2095", "P2055", "P2175")
BUDGET = "2000"
# Keep each filler turn small: the protected tail (last two turns) must stay
# well below ``trigger - 0.01`` of the *scaled* window, or ``_protect_boundary``
# hits its cap and legitimately rolls back (see the design notes).
FILLER_TURNS = 14
FILLER_CHARS = 200


# ---------------------------------------------------------------------------
# real search over a private copy of the on-disk index
# ---------------------------------------------------------------------------


def build_search_service(
    settings: Settings, products: list[Product], workdir: Path
) -> CatalogSearchService:
    """Real S3 search: real embedder, real Qdrant index, real rerank.

    A query costs exactly one embedding call; the catalog is never re-embedded
    because the disk index is reused. It is *copied* first: Qdrant's local mode
    takes an exclusive lock, and a running API server already holds the original.
    """
    if not (INDEX_DIR / "collection").exists():
        raise SystemExit(f"本地索引不存在：{INDEX_DIR}。先启动一次 API 生成索引，再跑本脚本。")
    index_dir = workdir / "qdrant-index"
    shutil.copytree(INDEX_DIR, index_dir)
    client = QdrantClient(path=str(index_dir))
    reranker = (
        FastembedReranker(settings.search_rerank_model) if settings.search_rerank_enabled else None
    )
    return CatalogSearchService(
        products,
        OpenAIEmbedder(settings),
        reranker=reranker,
        client=client,
    )


# ---------------------------------------------------------------------------
# middleware wrapper: record what actually reaches the model (S7-7 evidence)
# ---------------------------------------------------------------------------


class RecordingGovernance(ContextGovernanceMiddleware):
    """``ContextGovernanceMiddleware`` that keeps a copy of every forwarded payload."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.forwarded: list[list[Msg]] = []

    async def on_model_call(self, agent, input_kwargs, next_handler):
        async def recording(**kwargs: Any):
            self.forwarded.append(list(kwargs["messages"]))
            return await next_handler(**kwargs)

        return await super().on_model_call(agent, input_kwargs, recording)


class LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


_FILLER_DETAIL = (
    "帆布棉麻材质，耐磨抗造，透气网布，可调节肩带，侧边水瓶袋，"
    "适合短途出行与日常通勤，带安全扣，轻便易折叠。"
)


def _cny_sku(product: Product):
    return min(
        (sku for sku in product.skus if sku.price.currency == "CNY"),
        key=lambda sku: sku.price.amount,
        default=None,
    )


def _filler_hits(products: list[Product], index: int) -> list[dict[str, Any]]:
    """Real catalog rows, padded with realistic descriptions to add token weight."""
    by_id = {product.product_id: product for product in products}
    rows: list[dict[str, Any]] = []
    for product_id in CARD_PRODUCTS:
        product = by_id[product_id]
        sku = _cny_sku(product)
        assert sku is not None
        rows.append(
            {
                "product_id": product.product_id,
                "title": product.title,
                "brand": product.brand,
                "price_major": float(sku.price.amount),
                "currency": "CNY",
                "weight_kg": product.weight_kg,
                "highlights": [f"{h['label']}: {h['detail']}" for h in product.highlights],
                "description": product.description,
                "detail": (f"第{index}轮复核：" + _FILLER_DETAIL * 3)[:FILLER_CHARS],
            }
        )
    return rows


def _filler_turn(index: int, products: list[Product]) -> list[Msg]:
    """A long, coherent history turn built from real catalog data.

    The filler must look like the same conversation the agent is actually
    having. Placeholder rows ("OLD-…", "历史候选") poisoned the real run: the
    summarizer called everything a placeholder and the model then refused to
    recall the real card even though the appendix carried it.
    """
    body = f"第{index}轮：再帮我比较一下猫用的出行包，预算 2000 以内。"
    result = json.dumps(
        {"hits": _filler_hits(products, index), "total": len(CARD_PRODUCTS)},
        ensure_ascii=False,
    )
    return [
        UserMsg(name="user", content=body),
        AssistantMsg(name="purchase_agent", content=[TextBlock(text="我先查一下目录。")]),
        AssistantMsg(name="purchase_agent", content=[TextBlock(text=result)]),
        AssistantMsg(
            name="purchase_agent", content=[TextBlock(text="这几件都符合预算，你看看哪个合适。")]
        ),
    ]


def _seed_context(products: list[Product]) -> list[Msg]:
    context: list[Msg] = []
    for index in range(1, FILLER_TURNS + 1):
        context.extend(_filler_turn(index, products))
    return context


def _build_agent(
    settings: Settings,
    products: list[Product],
    search: CatalogSearchService,
    archive_root: Path,
) -> tuple[Agent, RecordingGovernance, SessionArchiveOffloader]:
    categories = sorted({product.category for product in products})
    model = build_chat_model(settings)
    model.context_size = CONTEXT_SIZE  # scale the window; ratios stay as configured

    offloader = SessionArchiveOffloader(archive_root)
    governance = RecordingGovernance(
        offloader=offloader,
        protect_turns=settings.context_protect_turns,
        gate_ratio=settings.context_gate_ratio,
    )
    agent = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for(categories, archive_enabled=True),
        model=model,
        toolkit=Toolkit([make_product_search_tool(search), make_read_archive_tool(offloader)]),
        middlewares=[governance],
        offloader=offloader,
        context_config=ContextConfig(
            trigger_ratio=settings.context_trigger_ratio,
            compression_fallback_to_truncation=False,
            summary_template=SUMMARY_TEMPLATE,
        ),
        react_config=ReActConfig(max_iters=8, interruption_raise_cancelled_error=True),
    )
    allow_product_search_tool(agent)
    return agent, governance, offloader


def _summary_text(summary: Any) -> str:
    if isinstance(summary, str):
        return summary
    return "\n".join(getattr(block, "text", "") or "" for block in summary or [])


def _answer_text(reply: Msg) -> str:
    structured = getattr(reply, "structured_output", None) or {}
    parts = [structured.get("summary", "")]
    parts.extend(
        f"{card.get('title', '')} {card.get('sku_id', '')}" for card in structured.get("cards", [])
    )
    parts.append(reply.get_text_content() or "")
    return "\n".join(part for part in parts if part)


async def _count(agent: Agent) -> int:
    prepared = await agent._prepare_model_input()
    return await agent.model.count_tokens(prepared["messages"], prepared.get("tools"))


def _normalize(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    collector = LogCollector()
    governance_log = logging.getLogger("harbor.agents.context_governance")
    governance_log.setLevel(logging.INFO)
    governance_log.addHandler(collector)

    settings = Settings()
    archive_root = settings.context_archive_dir / "_real-s7"
    shutil.rmtree(archive_root, ignore_errors=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    # Keep the index copy outside ``archive_root``: that tree is scanned for
    # ``*.json`` archive files, and the copy carries its own ``meta.json``.
    index_copy_root = settings.context_archive_dir / "_real-s7-index"
    shutil.rmtree(index_copy_root, ignore_errors=True)
    index_copy_root.mkdir(parents=True, exist_ok=True)

    products = load_products(DEFAULT_DATA_PATH)
    print("S7 真额度验收（真实 qwen-plus + 真实压缩 + 真实检索）")
    print(f"模型：{settings.openai_model} @ {settings.openai_base_url}")
    print(f"embedding：{settings.openai_embedding_model}（每次检索 1 次调用，索引复用）")
    print(
        f"窗口缩放到 {CONTEXT_SIZE}，trigger={settings.context_trigger_ratio}，"
        f"gate={settings.context_gate_ratio}"
    )
    print(f"归档目录：{archive_root}\n")

    search = build_search_service(settings, products, index_copy_root)
    agent, governance, _ = _build_agent(settings, products, search, archive_root)
    agent.state.context = _seed_context(products)

    prepared = await agent._prepare_model_input()
    seeded_tokens = await agent.model.count_tokens(prepared["messages"], prepared.get("tools"))
    print(
        f"[种子] {len(agent.state.context)} 条历史，约 {seeded_tokens} tokens"
        f"（闸门 {int(CONTEXT_SIZE * settings.context_gate_ratio)}）"
    )

    # -- Turn 1: real turn; compression of the seeded history happens inside ----
    print("\n[轮 1] 真实模型：预算 2000 以内，从目录里挑一件宠物出行用品")
    reply = await agent.reply(
        UserMsg(
            name="user",
            content=(
                f"我预算 {BUDGET} 元以内，想给猫买个出行用的东西。"
                "请从目录里挑一件最合适的，并给出它的 sku_id。"
            ),
        ),
        structured_schema=ProductCardList,
    )
    if not reply.structured_output:
        print("  ✗ 模型没有返回结构化输出", file=sys.stderr)
        print(f"  模型原文：{_answer_text(reply)}", file=sys.stderr)
        return 1
    cards = ProductCardList.model_validate(reply.structured_output)
    if not cards.cards:
        print("  ✗ 模型没有产出任何商品卡，无法继续 S7-3", file=sys.stderr)
        print(f"  模型原文：{_answer_text(reply)}", file=sys.stderr)
        return 1
    chosen = cards.cards[0]
    print(
        f"  模型推荐：{chosen.title}（{chosen.sku_id}，¥{chosen.price_major:g} {chosen.currency}）"
    )
    print(f"  summary：{cards.summary}")

    archives_after_turn_1 = sorted(archive_root.rglob("*.json"))
    summary_1 = _summary_text(agent.state.summary)
    print(f"  第一次压缩后摘要 {len(summary_1)} 字，归档 {len(archives_after_turn_1)} 份")
    print(f"  上下文剩余 {len(agent.state.context)} 条，约 {await _count(agent)} tokens")

    # S7-6 baseline: take it *after* a reply, when the structured-output tool is
    # registered. Before the first reply that tool does not exist yet, so the
    # prefix comparison would flag a difference unrelated to compression.
    prepared = await agent._prepare_model_input()
    system_before = prepared["messages"][0].get_text_content()
    tools_before = [tool.get("function", {}).get("name") for tool in prepared.get("tools") or []]
    tools_payload_before = json.dumps(prepared.get("tools"), ensure_ascii=False)

    # -- Turn 2: push the card out of the protected tail, then ask it back -----
    for index in range(FILLER_TURNS + 1, FILLER_TURNS * 2 + 1):
        agent.state.context.extend(_filler_turn(index, products))
    before_turn_2_tokens = await _count(agent)
    print(f"\n[轮 2] 再灌历史至约 {before_turn_2_tokens} tokens，问预算与 product_id")
    reply = await agent.reply(
        UserMsg(
            name="user",
            content=(
                "我预算多少？刚才推荐的 product_id 是哪个？我要接着下单，直接告诉我数字和编号。"
            ),
        ),
        structured_schema=ProductCardList,
    )
    answer = _normalize(_answer_text(reply))
    print(f"  模型回答：{answer}")

    archives = sorted(archive_root.rglob("*.json"))
    card_archived = any(chosen.sku_id in path.read_text(encoding="utf-8") for path in archives)
    summary_2 = _summary_text(agent.state.summary)
    print(f"  压缩后摘要（{len(summary_2)} 字）：")
    for line in summary_2.splitlines():
        print(f"    | {line}")

    # -- evidence -------------------------------------------------------------
    print("\n=== 判定 ===")
    checks: list[tuple[str, bool, str]] = []

    headings = ("# 目标", "# 当前进展", "# 关键事实", "# 下一步", "# 必须保留")
    missing = [heading for heading in headings if heading not in summary_2]
    checks.append(("S7-2 摘要五字段", not missing, f"缺 {missing}" if missing else "五段齐全"))

    checks.append(
        (
            "S7-3 预算与 product_id 答对",
            BUDGET in answer and chosen.sku_id in answer,
            f"预算 {'✓' if BUDGET in answer else '✗'}，SKU "
            f"{'✓' if chosen.sku_id in answer else '✗'}",
        )
    )
    checks.append(
        (
            "S7-3 卡片确实被归档",
            card_archived,
            f"{chosen.sku_id} 出现在 {len(archives)} 份归档中"
            if card_archived
            else "归档里找不到该 SKU",
        )
    )

    ok_lines = [line for line in collector.lines if "context compression ok" in line]
    telemetry = bool(ok_lines) and all(
        re.search(r"tokens \d+ -> \d+", line) and re.search(r"\d+\.\d ms", line)
        for line in ok_lines
    )
    checks.append(("S7-4 压缩遥测", telemetry, f"{len(ok_lines)} 行"))
    for line in collector.lines:
        if "compression" in line or "rejected" in line:
            print(f"    · {line}")

    prepared_after = await agent._prepare_model_input()
    system_after = prepared_after["messages"][0].get_text_content()
    tools_payload_after = json.dumps(prepared_after.get("tools"), ensure_ascii=False)
    same_system = system_before == system_after
    same_tools = tools_payload_before == tools_payload_after
    if same_system and same_tools:
        detail = "system 与 tools 逐字不变"
    else:
        names_after = [
            tool.get("function", {}).get("name") for tool in prepared_after.get("tools") or []
        ]
        detail = (
            f"system {'不变' if same_system else '变了'}；"
            f"tools {'不变' if same_tools else f'变了 {tools_before} -> {names_after}'}"
        )
    checks.append(("S7-6 前缀稳定", same_system and same_tools, detail))

    injected = governance.forwarded[-1][-1] if governance.forwarded else None
    injected_text = injected.get_text_content() if injected is not None else ""
    checks.append(
        (
            "S7-7 偏好注入在消息末尾",
            injected is not None
            and injected.role == "user"
            and BUDGET in injected_text
            and len(governance.forwarded[-1]) > 1,
            f"最后一条 role={getattr(injected, 'role', None)}，含预算 "
            f"{'✓' if BUDGET in injected_text else '✗'}",
        )
    )

    # S7-5: legacy build is the pre-S7 baseline; one real turn proves it runs.
    legacy = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for(
            sorted({product.category for product in products}), archive_enabled=False
        ),
        model=agent.model,
        toolkit=Toolkit([make_product_search_tool(search)]),
        react_config=ReActConfig(max_iters=5, interruption_raise_cancelled_error=True),
    )
    allow_product_search_tool(legacy)
    legacy_reply = await legacy.reply(
        UserMsg(name="user", content="给我推荐一个宠物出行包"),
        structured_schema=ProductCardList,
    )
    legacy_ok = bool(getattr(legacy_reply, "structured_output", None))
    checks.append(
        (
            "S7-5 legacy 基线可运行",
            legacy_ok and legacy.offloader is None and legacy._reply_middlewares == [],
            "无中间件/无 offloader，真实轮返回结构化结果",
        )
    )

    for name, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'} {name} — {detail}")

    print("\n归档文件：")
    for path in archives:
        print(f"  {path}")

    failed = [name for name, passed, _ in checks if not passed]
    print()
    shutil.rmtree(index_copy_root, ignore_errors=True)
    if failed:
        print(f"失败：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
