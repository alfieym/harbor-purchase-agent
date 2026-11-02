"""Interactive REPL: the model calls ``product_search`` and returns product cards (S4).

Usage:
    uv run --directory backend python -m harbor.agents

Requires a model API key (``OPENAI_API_KEY`` / ``HARBOR_OPENAI_API_KEY``). Building the
index needs a ``/embeddings``-capable endpoint (query-time embedding failure still
degrades to keyword_2gram, but indexing itself needs the embedder once).

The index is persisted under ``.cache/catalog-index/`` so restarts skip embedding.
A fingerprint of the catalog file + embedding config + index mode invalidates it on
change. Qdrant local mode locks that directory, so run one CLI at a time.
"""

import asyncio
import hashlib
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from agentscope.agent import Agent, ReActConfig
from agentscope.message import UserMsg
from agentscope.tool import Toolkit
from qdrant_client import QdrantClient

from harbor.agents.cards import ProductCardList
from harbor.agents.models import build_chat_model
from harbor.agents.permissions import allow_product_search_tool
from harbor.agents.prompts import system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings, get_settings
from harbor.features.catalog.models import Product
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.embedder import Embedder, OpenAIEmbedder
from harbor.features.catalog.search.rerank import FastembedReranker
from harbor.features.catalog.search.service import CatalogSearchService

DEFAULT_DATA_PATH = Path(__file__).resolve().parents[4] / "data" / "catalog-v1.jsonl"
INDEX_CACHE_DIR = Path(__file__).resolve().parents[4] / ".cache" / "catalog-index"
INDEX_DIR = INDEX_CACHE_DIR / "qdrant"
FINGERPRINT_PATH = INDEX_CACHE_DIR / "fingerprint.txt"
COLLECTION = "products"


class _HashedEmbedder:
    """Deterministic local vectors, only used to build the index.

    DeepSeek's endpoint has no ``/embeddings``. The index still needs dense
    vectors of a fixed size, so we fill them locally; query-time embedding goes
    to the real embedder, fails, and the S3 chain degrades to ``keyword_2gram``.
    """

    def __init__(self, dim: int = 256) -> None:
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


def _probe_embedder(embedder: OpenAIEmbedder) -> str | None:
    """Return None when the endpoint serves ``/embeddings``, else a short reason."""
    try:
        embedder.embed(["probe"])
    except Exception as exc:  # noqa: BLE001 — the probe decides the degraded mode
        return str(exc).splitlines()[0]
    return None


def _embedders(settings: Settings) -> tuple[OpenAIEmbedder, Embedder | None]:
    """Build the query embedder plus an index-only stand-in when the endpoint is down.

    The degraded index must not be reused once the endpoint recovers, so the
    caller records the mode in the fingerprint.
    """
    embedder = OpenAIEmbedder(settings)
    error = _probe_embedder(embedder)
    if error is None:
        return embedder, None
    print(
        f"提示：embedding 端点不可用（{error}）。查询将降级为 keyword_2gram。",
        file=sys.stderr,
    )
    return embedder, _HashedEmbedder()


def _catalog_fingerprint(data_path: Path, settings: Settings, degraded: bool) -> str:
    """Hash the catalog file plus the embedding setup; changes invalidate the index."""
    digest = hashlib.sha256()
    digest.update(data_path.read_bytes())
    digest.update(settings.openai_embedding_model.encode("utf-8"))
    digest.update(settings.openai_base_url.encode("utf-8"))
    # A local-vector index and a real-embedding index are not interchangeable.
    digest.update(b"local-fallback" if degraded else b"remote-embeddings")
    return digest.hexdigest()


def _open_index(data_path: Path, settings: Settings, degraded: bool) -> tuple[QdrantClient, str]:
    """Open the on-disk index, dropping it when the catalog or embedder changed."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(INDEX_DIR))
    fingerprint = _catalog_fingerprint(data_path, settings, degraded)
    cached = (
        FINGERPRINT_PATH.read_text(encoding="utf-8").strip() if FINGERPRINT_PATH.exists() else None
    )
    indexed = client.collection_exists(COLLECTION) and client.count(COLLECTION).count > 0
    if indexed and cached == fingerprint:
        print("提示：复用本地索引（跳过 embedding）。", file=sys.stderr)
    elif indexed:
        print("提示：商品目录或 embedding 配置已变化，重建本地索引。", file=sys.stderr)
        client.delete_collection(COLLECTION)
    else:
        print("提示：首次建索引，约 4–5 分钟（3700 条商品）。", file=sys.stderr)
    return client, fingerprint


def build_agent(
    settings: Settings,
    products: list[Product],
    client: QdrantClient | None = None,
    embedders: tuple[OpenAIEmbedder, Embedder | None] | None = None,
) -> Agent:
    """Assemble the purchase agent: S3 search tool + chat model + ReAct loop."""
    embedder, index_embedder = embedders if embedders is not None else _embedders(settings)
    reranker = (
        FastembedReranker(settings.search_rerank_model) if settings.search_rerank_enabled else None
    )
    service = CatalogSearchService(
        products,
        embedder,
        reranker=reranker,
        index_embedder=index_embedder,
        client=client,
    )
    tool = make_product_search_tool(service)

    model = build_chat_model(settings)

    agent = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for([product.category for product in products]),
        model=model,
        toolkit=Toolkit([tool]),
        react_config=ReActConfig(max_iters=15),
    )
    # Without this the REPL parks every tool call on a permission prompt.
    allow_product_search_tool(agent)
    return agent


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
    data_file = data_path or DEFAULT_DATA_PATH
    products = load_products(data_file)
    embedders = _embedders(settings)
    client, fingerprint = _open_index(data_file, settings, degraded=embedders[1] is not None)
    agent = build_agent(settings, products, client=client, embedders=embedders)
    FINGERPRINT_PATH.write_text(fingerprint, encoding="utf-8")
    asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
