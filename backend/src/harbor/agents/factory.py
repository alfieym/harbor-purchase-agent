"""Shared agent assembly: one runtime, many session agents (S5).

The CLI and the HTTP chat endpoint both need the same expensive read-only pieces —
products, the on-disk Qdrant index, the reranker, the chat model. ``build_runtime``
assembles those once; ``build_agent`` makes a fresh per-session ``Agent`` (which
carries its own ReAct conversation state) on top of the shared runtime.
"""

import hashlib
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agentscope.agent import Agent, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from qdrant_client import QdrantClient

from harbor.agents.models import build_chat_model
from harbor.agents.permissions import allow_product_search_tool
from harbor.agents.prompts import system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
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

    When the configured endpoint has no ``/embeddings`` the index still needs dense
    vectors of a fixed size, so we fill them locally; query-time embedding goes to
    the real embedder, fails, and the S3 chain degrades to ``keyword_2gram``.
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
    """Build the query embedder plus an index-only stand-in when the endpoint is down."""
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


@dataclass
class AgentRuntime:
    """Shared, read-only components assembled once at startup (S5)."""

    settings: Settings
    products: list[Product]
    service: CatalogSearchService
    model: ChatModelBase
    categories: list[str]


def build_runtime(settings: Settings, data_path: Path | None = None) -> AgentRuntime:
    """Assemble the shared read-only pieces (products + index + service + model).

    The on-disk index fingerprint is written once the index is built, so the next
    process reuses it instead of re-embedding the catalog.
    """
    data_file = data_path or DEFAULT_DATA_PATH
    products = load_products(data_file)
    embedder, index_embedder = _embedders(settings)
    client, fingerprint = _open_index(data_file, settings, degraded=index_embedder is not None)
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
    model = build_chat_model(settings)
    FINGERPRINT_PATH.write_text(fingerprint, encoding="utf-8")
    return AgentRuntime(
        settings=settings,
        products=products,
        service=service,
        model=model,
        categories=sorted({product.category for product in products}),
    )


def build_agent(runtime: AgentRuntime, state: AgentState | None = None) -> Agent:
    """Assemble a fresh per-session Agent on top of the shared runtime.

    ``state`` restores a previous session's full conversation (S6): the store
    passes the snapshot it loaded from disk, or ``None`` for a brand-new session.
    """
    tool = make_product_search_tool(runtime.service)
    agent = Agent(
        name="purchase_agent",
        system_prompt=system_prompt_for(runtime.categories),
        model=runtime.model,
        toolkit=Toolkit([tool]),
        state=state,
        # ``interruption_raise_cancelled_error=True``: when ``run_turn``'s
        # ``asyncio.wait_for`` cancels a turn at the timeout budget, AgentScope
        # otherwise swallows the CancelledError and returns an "INTERRUPTED"
        # reply — so no TimeoutError surfaces and the timeout silently becomes
        # a 502. Re-raising keeps the timeout path clean (S5-2).
        react_config=ReActConfig(
            max_iters=15,
            interruption_raise_cancelled_error=True,
        ),
    )
    # Without this the loop parks every tool call on a permission prompt.
    allow_product_search_tool(agent)
    return agent
