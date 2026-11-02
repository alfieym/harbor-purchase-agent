"""Interactive REPL: the model calls ``product_search`` and returns product cards (S4).

Usage:
    uv run --directory backend python -m harbor.agents

Requires a model API key (``OPENAI_API_KEY`` / ``HARBOR_OPENAI_API_KEY``). Building the
index needs a ``/embeddings``-capable endpoint (query-time embedding failure still
degrades to keyword_2gram, but indexing itself needs the embedder once).

The index is persisted under ``.cache/catalog-index/`` so restarts skip embedding.
A fingerprint of the catalog file + embedding config + index mode invalidates it on
change. Qdrant local mode locks that directory, so run one CLI at a time.

All assembly lives in ``harbor.agents.factory`` (S5), shared with the HTTP chat
endpoint. This module keeps only the interactive REPL loop.
"""

import asyncio
import sys
from pathlib import Path

from agentscope.agent import Agent
from agentscope.message import UserMsg

from harbor.agents.cards import ProductCardList
from harbor.agents.factory import build_agent, build_runtime
from harbor.core.config import get_settings


def _print_cards(cards: ProductCardList) -> None:
    print(f"\n助手：{cards.summary}")
    for index, card in enumerate(cards.cards, start=1):
        tags = " / ".join(card.material_tags) if card.material_tags else "—"
        print(
            f"  {index}. {card.title}  [{card.brand}]  "
            f"{card.price_major:g} {card.currency} · {card.weight_kg:g}kg · "
            f"{card.category} · {tags}"
        )


async def run_repl(agent: Agent) -> None:
    print("Harbor 采购助手（输入 /quit 退出）")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text or text in {"/quit", "/exit"}:
            break
        try:
            reply = await agent.reply(
                UserMsg(name="user", content=text), structured_schema=ProductCardList
            )
        except Exception as exc:  # noqa: BLE001 — surface model errors, keep the loop alive
            print(f"\n出错了：{exc}")
            continue
        structured = reply.structured_output
        if structured is None:
            print(f"\n助手（文本）：{reply.get_text_content()}")
            continue
        _print_cards(ProductCardList.model_validate(structured))


def main(data_path: Path | None = None) -> None:
    settings = get_settings()
    if not settings.llm_configured:
        print(
            "未配置模型 API key。请设置 OPENAI_API_KEY（或 HARBOR_OPENAI_API_KEY）。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    runtime = build_runtime(settings, data_path=data_path)
    asyncio.run(run_repl(build_agent(runtime)))


if __name__ == "__main__":
    main()
