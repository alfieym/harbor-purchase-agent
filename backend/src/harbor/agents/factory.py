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

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import ToolBase, Toolkit
from qdrant_client import QdrantClient

from harbor.agents.context_governance import (
    ContextGovernanceMiddleware,
    SessionArchiveOffloader,
    make_read_archive_tool,
)
from harbor.agents.dispatch import (
    AgentSubAgentRunner,
    make_task_dispatch_tool,
    read_dispatch_ledger,
)
from harbor.agents.models import build_chat_model
from harbor.agents.order_tools import make_order_tools
from harbor.agents.permissions import (
    allow_dispatch_tool,
    allow_order_tools,
    allow_product_search_tool,
)
from harbor.agents.preferences import (
    IDENTITY_STATE_KEY,
    PreferenceMemory,
    make_preference_tools,
)
from harbor.agents.prompts import SUMMARY_TEMPLATE, system_prompt_for
from harbor.agents.tools import make_product_search_tool
from harbor.core.config import Settings
from harbor.features.catalog.models import Product
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.embedder import Embedder, OpenAIEmbedder
from harbor.features.catalog.search.rerank import FastembedReranker
from harbor.features.catalog.search.service import CatalogSearchService
from harbor.features.memory.sqlite_store import SqliteMemoryStore
from harbor.features.orders.service import OrderService
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.runtime import GuardRuntime
from harbor.guardrails.tools import wrap_tools

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
    # S8: preference writes embed through the same endpoint as search.
    embedder: Embedder
    memory_store: SqliteMemoryStore | None
    # S11: ``None`` means the kill switch is off — no budget, no circuits, no
    # tool wrapping, exactly the pre-S11 behaviour.
    guard: GuardRuntime | None = None


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
    # S11: one GuardedChatModel covers the main agent, S9 sub-agents and S7
    # summary calls, since all of them are handed this same object.
    guard = GuardRuntime.from_settings(settings) if settings.guardrails_enabled else None
    wrapped_model = model
    if guard is not None:
        wrapped_model = GuardedChatModel(
            model,
            guard,
            request_timeout=settings.guard_request_timeout_seconds,
            max_retries=settings.guard_max_retries,
            backoff=settings.guard_retry_backoff_seconds,
        )
    FINGERPRINT_PATH.write_text(fingerprint, encoding="utf-8")
    return AgentRuntime(
        settings=settings,
        products=products,
        service=service,
        model=wrapped_model,
        categories=sorted({product.category for product in products}),
        embedder=embedder,
        memory_store=SqliteMemoryStore(settings.memory_db_path),
        guard=guard,
    )


def _identity_from(
    state: AgentState | None,
    buyer_id: str | None,
    shopping_session_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve both ids: the caller wins, else the identity the store injected."""
    stored = state.middle_context.get(IDENTITY_STATE_KEY) if state is not None else None
    if not isinstance(stored, dict):
        stored = {}
    buyer = buyer_id or stored.get("buyer_id")
    session = shopping_session_id or stored.get("shopping_session_id")
    return (
        buyer if isinstance(buyer, str) and buyer else None,
        session if isinstance(session, str) and session else None,
    )


def build_agent(
    runtime: AgentRuntime,
    state: AgentState | None = None,
    *,
    buyer_id: str | None = None,
    shopping_session_id: str | None = None,
    order_service: OrderService | None = None,
) -> Agent:
    """Assemble a fresh per-session Agent on top of the shared runtime.

    ``state`` restores a previous session's full conversation (S6): the store
    passes the snapshot it loaded from disk, or a fresh state. ``buyer_id`` and
    ``shopping_session_id`` come from that state's injected identity; the
    explicit arguments are for callers without a store (the CLI), and override
    it. ``order_service`` (S10) enables the transaction tools; ``None`` keeps the
    pre-S10 read-only agent.
    """
    settings = runtime.settings
    # S7-5: ``legacy`` is the pre-S7 baseline — no middleware, no archive tool,
    # and AgentScope's own compression defaults. ``governed`` wraps every
    # compression and every model call with the S7 guarantees.
    governed = settings.context_strategy == "governed"
    # S11: one guard session per agent — the loop window and sequence history
    # belong to this conversation, not to the shared runtime.
    guard_session = runtime.guard.new_session() if runtime.guard is not None else None

    def guarded(items: list[ToolBase]) -> list[ToolBase]:
        if runtime.guard is None or guard_session is None:
            return items
        return list(
            wrap_tools(
                items,
                session=guard_session,
                metrics=runtime.guard.metrics,
                max_chars=settings.guard_tool_output_max_chars,
                switches=runtime.guard.switches,
                circuits=runtime.guard.tool_circuits,
            )
        )

    buyer_id, shopping_session_id = _identity_from(state, buyer_id, shopping_session_id)
    if buyer_id is not None and shopping_session_id is not None:
        # Write the resolved pair back so the tools (which read state, not
        # function arguments) see the same identity the rest of the agent does.
        if state is None:
            state = AgentState()
        identity = dict(state.middle_context.get(IDENTITY_STATE_KEY) or {})
        identity["buyer_id"] = buyer_id
        identity["shopping_session_id"] = shopping_session_id
        state.middle_context[IDENTITY_STATE_KEY] = identity
    # S10: the kill switch removes the tools as well as the recorder, so the
    # model cannot ask for a capability the application layer will not honour.
    orders_enabled = order_service is not None and settings.transaction_enabled
    # S9: dispatch is a governed-only capability; the ledger restores the last
    # persisted plan so unfinished tasks are not silently forgotten.
    dispatch_enabled = governed and settings.dispatch_enabled
    ledger = read_dispatch_ledger(state) if dispatch_enabled else None
    prompt = system_prompt_for(
        runtime.categories,
        archive_enabled=governed,
        preferences_enabled=governed,
        dispatch_enabled=dispatch_enabled,
        orders_enabled=orders_enabled,
        dispatch_ledger=ledger,
    )
    # S8: without a buyer id the turn/session scopes still work; the memory
    # simply reports ``long_term`` as unavailable instead of failing writes.
    # Built before the search tool because that tool applies material dislikes.
    memory = (
        PreferenceMemory(
            buyer_id=buyer_id,
            store=runtime.memory_store,
            embedder=runtime.embedder,
            vector_model=settings.openai_embedding_model,
        )
        if governed
        else None
    )
    # The ``None`` check (not just ``orders_enabled``) is what narrows the type.
    active_orders = order_service if orders_enabled else None
    tool = make_product_search_tool(
        runtime.service,
        memory=memory,
        # S10: only the main agent records evidence; the S9 sub-agent runner
        # builds its own search tool and must not write for the parent session.
        evidence_recorder=(active_orders.record_search_hits if active_orders is not None else None),
    )
    order_tools = make_order_tools(active_orders) if active_orders is not None else []
    if not governed:
        agent = Agent(
            name="purchase_agent",
            system_prompt=prompt,
            model=runtime.model,
            toolkit=Toolkit(guarded([tool, *order_tools])),
            state=state,
            # ``interruption_raise_cancelled_error=True``: when ``run_turn``'s
            # ``asyncio.wait_for`` cancels a turn at the timeout budget,
            # AgentScope otherwise swallows the CancelledError and returns an
            # "INTERRUPTED" reply — so no TimeoutError surfaces and the timeout
            # silently becomes a 502. Re-raising keeps the timeout path clean.
            react_config=ReActConfig(
                max_iters=15,
                interruption_raise_cancelled_error=True,
            ),
        )
    else:
        # S7: every compression is archived + validated, and every model call is
        # measured against the hard gate before it leaves the process.
        offloader = SessionArchiveOffloader(settings.context_archive_dir)
        assert memory is not None
        governance = ContextGovernanceMiddleware(
            offloader=offloader,
            protect_turns=settings.context_protect_turns,
            gate_ratio=settings.context_gate_ratio,
            preferences=memory,
        )
        tools: list[ToolBase] = [
            tool,
            make_read_archive_tool(offloader),
            *make_preference_tools(memory),
            *order_tools,
        ]
        if dispatch_enabled:
            runner = AgentSubAgentRunner(
                model=runtime.model,
                service=runtime.service,
                categories=runtime.categories,
                memory=memory,
                guard=runtime.guard,
            )
            tools.append(
                make_task_dispatch_tool(
                    memory,
                    runner,
                    max_concurrency=settings.dispatch_max_concurrency,
                    task_timeout_seconds=settings.dispatch_task_timeout_seconds,
                    batch_timeout_seconds=settings.dispatch_batch_timeout_seconds,
                    max_tasks=settings.dispatch_max_tasks,
                )
            )
        agent = Agent(
            name="purchase_agent",
            system_prompt=prompt,
            model=runtime.model,
            toolkit=Toolkit(guarded(tools)),
            middlewares=[governance],
            state=state,
            offloader=offloader,
            context_config=ContextConfig(
                trigger_ratio=settings.context_trigger_ratio,
                compression_fallback_to_truncation=False,
                summary_template=SUMMARY_TEMPLATE,
            ),
            # ``interruption_raise_cancelled_error=True``: same S5-2 reason as
            # the legacy branch above.
            react_config=ReActConfig(
                max_iters=15,
                interruption_raise_cancelled_error=True,
            ),
        )
    # Without this the loop parks every tool call on a permission prompt.
    allow_product_search_tool(agent)
    if dispatch_enabled:
        allow_dispatch_tool(agent)
    if orders_enabled:
        allow_order_tools(agent)
    # S11: ``run_turn`` reads this to reset the turn window and to short-circuit
    # an exhausted budget before the agent is even called.
    agent.guard_session = guard_session
    return agent
