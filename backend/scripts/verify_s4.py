"""Manually verify S4 end-to-end: product_search tool → real model → product cards.

Usage:
    uv run --directory backend python scripts/verify_s4.py
    uv run --directory backend python scripts/verify_s4.py --real-catalog

What it checks:

    ① 工具（离线）        product_search 返回模型可读 JSON，硬约束按预期过滤
    ② 商品卡（离线）      ProductCardList schema round-trip
    ③ 结构化输出（真模型） 模型调用工具后返回合法的 ProductCardList
    ④ 空结果（真模型）     无命中时模型如实告知，不编造商品
    ⑤ 未编造校验          每张卡的 sku_id 必须来自工具返回的 hits

默认用内置的 4 条测试商品；``--real-catalog`` 会加载 ``data/catalog-v1.jsonl``
（3700 条），该模式需要 ``/embeddings`` 能用的端点（DeepSeek 默认端点没有）。
退出码：0 = 全部通过，1 = 有失败项。
"""

import argparse
import asyncio
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from agentscope.agent import Agent, ReActConfig
from agentscope.message import UserMsg
from agentscope.tool import Toolkit

from harbor.agents.cards import ProductCard, ProductCardList
from harbor.agents.cli import FINGERPRINT_PATH, _open_index
from harbor.agents.models import build_chat_model
from harbor.agents.permissions import allow_product_search_tool
from harbor.agents.prompts import system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings, get_settings
from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.embedder import OpenAIEmbedder
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.catalog.search.service import CatalogSearchService

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog-v1.jsonl"


class FakeEmbedder:
    """Deterministic character-frequency embedder; no network, no model."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            counts = [0.0] * self._dim
            for char in text:
                counts[ord(char) % self._dim] += 1.0
            norm = math.sqrt(sum(value * value for value in counts)) or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


class RecordingService:
    """Wraps CatalogSearchService and remembers every tool result."""

    def __init__(self, service: CatalogSearchService) -> None:
        self._service = service
        self.results: list[SearchResult] = []

    def search(self, query_text: str, spec: ProductSearchSpec, limit: int = 10) -> SearchResult:
        result = self._service.search(query_text, spec, limit=limit)
        self.results.append(result)
        return result


def _product(pid: str, title: str, category: str, price: float, weight: float) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=title,
        brand="TestBrand",
        category=category,
        tax_category=category,
        description="",
        highlights=[{"label": "卖点", "detail": "轻便"}],
        material_tags=["ABS塑料"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=weight,
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


PRODUCTS = [
    _product("P1", "无线吸尘器 轻便", "家用电器", 300, 1.0),
    _product("P2", "旗舰吸尘器 大吸力", "家用电器", 4000, 1.0),  # 超预算
    _product("P3", "北欧台灯", "家用电器", 200, 0.5),
    _product("P4", "猫砂盆", "母婴宠物", 150, 2.0),  # 类目不符
]

SMALL_CATALOG_PROMPTS: list[tuple[str, str, bool]] = [
    ("正常查询", "预算500元，给养猫家庭买个吸尘器", True),
    ("无命中查询", "预算1元买个吸尘器", False),
]

# The real catalog has no vacuums, but it does have pet waterers — use a query
# that can actually hit, otherwise the "正常查询" check is unsatisfiable.
REAL_CATALOG_PROMPTS: list[tuple[str, str, bool]] = [
    ("正常查询", "预算300元，给猫买个饮水器", True),
    ("无命中查询", "预算1元买个猫饮水器", False),
]


def probe_embeddings(settings: Settings) -> str | None:
    """Return None when the configured endpoint serves /embeddings, else the error."""
    try:
        OpenAIEmbedder(settings, timeout=10.0).embed(["probe"])
    except Exception as exc:  # noqa: BLE001 — probe is informational
        return f"{type(exc).__name__}: {exc}"
    return None


def fabricate_problems(cards: ProductCardList, tool_results: Sequence[SearchResult]) -> list[str]:
    """⑤ 校验模型没有编造商品：每张卡的 ``sku_id`` 必须来自工具返回的 hits。

    返回人类可读的问题列表；空列表表示没有编造。
    """
    legitimate_skus = {hit.selected_sku.sku_id for result in tool_results for hit in result.hits}
    return [
        f"卡片 sku={card.sku_id!r} 不在工具命中里（疑似编造）"
        for card in cards.cards
        if card.sku_id not in legitimate_skus
    ]


def _build_agent(
    settings: Settings, service: CatalogSearchService, products: Sequence[Product]
) -> Agent:
    tool = make_product_search_tool(service)
    agent = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for([product.category for product in products]),
        model=build_chat_model(settings),
        toolkit=Toolkit([tool]),
        react_config=ReActConfig(max_iters=15),
    )
    allow_product_search_tool(agent)
    return agent


def _print_cards(cards: ProductCardList) -> None:
    print(f"     summary: {cards.summary}")
    for index, card in enumerate(cards.cards, start=1):
        print(
            f"     {index}. {card.title} [{card.brand}] "
            f"{card.price_major:g} {card.currency} · sku={card.sku_id}"
        )


async def _model_checks(
    settings: Settings,
    service: CatalogSearchService,
    products: Sequence[Product],
    prompts: Sequence[tuple[str, str, bool]],
) -> list[str]:
    problems: list[str] = []
    recorder = RecordingService(service)
    agent = _build_agent(settings, recorder, products)

    for label, prompt, expect_cards in prompts:
        recorder.results.clear()
        print(f"\n④ [{label}] 你：{prompt}")
        reply = await agent.reply(
            UserMsg(name="user", content=prompt), structured_schema=ProductCardList
        )
        structured = reply.structured_output
        if structured is None:
            problems.append(f"[{label}] 未返回结构化输出；模型文本 = {reply.get_text_content()!r}")
            continue
        cards = ProductCardList.model_validate(structured)
        _print_cards(cards)

        if expect_cards and not cards.cards:
            problems.append(f"[{label}] 应命中商品，实际 cards 为空")
        if not cards.summary.strip():
            problems.append(f"[{label}] summary 为空")

        problems += [f"[{label}] {item}" for item in fabricate_problems(cards, recorder.results)]

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Manually verify S4")
    parser.add_argument(
        "--real-catalog",
        action="store_true",
        help="用 data/catalog-v1.jsonl（3700 条）和真实 embedding 端点，而不是内置测试商品",
    )
    args = parser.parse_args()

    problems: list[str] = []
    settings = get_settings()

    # ---- ① 工具（离线，确定性商品）----
    service = CatalogSearchService(PRODUCTS, FakeEmbedder())
    tool = make_product_search_tool(service)
    print(f"① 工具注册名 = {tool.name}\n")

    chunk = tool._func(  # type: ignore[attr-defined]
        query="养猫家庭用的轻便吸尘器", category="家用电器", price_max_major=500.0
    )
    payload = json.loads(chunk.content[0].text)
    print("② 模型调用 product_search 后读到的 JSON：")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    hit_ids = {hit["product_id"] for hit in payload["hits"]}
    filtered = {c["product_id"]: c["reasons"] for c in payload["filtered_out"]}
    if "P1" not in hit_ids:
        problems.append("P1（吸尘器，¥300）应命中")
    if "P2" not in filtered or "price_over" not in filtered.get("P2", []):
        problems.append("P2（¥4000）应因 price_over 被过滤")
    if "P4" not in filtered or "category_mismatch" not in filtered.get("P4", []):
        problems.append("P4（母婴宠物）应因 category_mismatch 被过滤")

    # ---- ② 商品卡 round-trip（离线）----
    card = ProductCard(
        title="无线吸尘器 轻便",
        brand="TestBrand",
        category="家用电器",
        price_major=300.0,
        currency="CNY",
        weight_kg=1.0,
        material_tags=["ABS塑料"],
        highlights=["卖点: 轻便"],
        sku_id="P1-S1",
        score=0.9,
    )
    cards = ProductCardList(cards=[card], summary="命中 1 件，1 件超预算被过滤")
    print("\n③ 商品卡 schema round-trip：")
    print(cards.model_dump_json(indent=2))

    # ---- ③ 真实模型 ----
    print()
    if not settings.llm_configured:
        print("④ 未配置模型 key（OPENAI_API_KEY / HARBOR_OPENAI_API_KEY）—— 跳过真实模型部分")
        print("    设 key 后重跑即可看到 ④⑤")
    else:
        if args.real_catalog:
            error = probe_embeddings(settings)
            if error is not None:
                print(f"④ --real-catalog 需要 /embeddings 端点，探测失败：{error}")
                raise SystemExit(1)
            products = load_products(DATA_PATH)
            print(f"④ 真实目录：{len(products)} 条商品，embedding 端点可用")
            # Reuse the CLI's on-disk index (`.cache/catalog-index/`) so the 3700-item
            # dense embedding build is paid once, not on every verify run.
            client, fingerprint = _open_index(DATA_PATH, settings, degraded=False)
            model_service = CatalogSearchService(products, OpenAIEmbedder(settings), client=client)
            FINGERPRINT_PATH.write_text(fingerprint, encoding="utf-8")
            prompts = REAL_CATALOG_PROMPTS
        else:
            print("④ 内置测试商品 + 确定性 embedder（加 --real-catalog 可跑真实目录）")
            products = PRODUCTS
            model_service = service
            prompts = SMALL_CATALOG_PROMPTS
        problems += asyncio.run(_model_checks(settings, model_service, products, prompts))

    print(f"\n{'=' * 60}\nS4: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")
    raise SystemExit(0 if not problems else 1)


if __name__ == "__main__":
    main()
