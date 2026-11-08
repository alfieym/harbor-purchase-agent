"""The ``product_search`` AgentScope tool wrapping the S3 search (S4).

The tool is a thin adapter: the model supplies ``ProductSearchArgs``, this module
builds a ``ProductSearchSpec``, calls ``CatalogSearchService.search``, and returns
the ``SearchResult`` as a compact JSON chunk the model can read.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from agentscope.message import TextBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk
from pydantic import BaseModel, Field

from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.search.fx import convert
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.orders.models import SearchEvidence
from harbor.observability import incr, publish_event, span

logger = logging.getLogger(__name__)

EvidenceRecorder = Callable[..., Awaitable[None]]


class ProductSearchArgs(BaseModel):
    """Arguments the model supplies when it calls ``product_search``."""

    query: str = Field(description="自然语言查询，例如「给猫用的饮水器」")
    category: str | None = Field(
        default=None,
        description="商品类目，如「母婴宠物」「家居生活」。用户没提类目时不要填，也不要臆测",
    )
    price_max_major: float | None = Field(default=None, description="预算上限（主币单位）")
    currency: str = Field(default="CNY", description="目标币种，ISO-4217 三位码")
    weight_max_kg: float | None = Field(default=None, description="重量上限（kg）")
    excluded_material_tags: list[str] = Field(default_factory=list, description="要排除的材质标签")


def _search_result_to_dict(result: SearchResult, target_currency: str) -> dict:
    """Reduce a ``SearchResult`` to a compact, model-friendly structure.

    Prices are converted to ``target_currency`` (the spec's currency) before the
    model sees them. The catalog is multi-currency (CNY/USD/EUR/JPY/SGD), so raw
    SKU prices vary; reporting a single target currency stops the model from
    guessing conversions on its own (S4-7).
    """
    return {
        "hits": [
            {
                "product_id": hit.product.product_id,
                "title": hit.product.title,
                "brand": hit.product.brand,
                "category": hit.product.category,
                "price_major": float(
                    convert(
                        hit.selected_sku.price.amount,
                        hit.selected_sku.price.currency,
                        target_currency,
                    )
                ),
                "currency": target_currency,
                "weight_kg": hit.product.weight_kg,
                "material_tags": hit.product.material_tags,
                "highlights": [f"{h['label']}: {h['detail']}" for h in hit.product.highlights],
                "selected_sku_id": hit.selected_sku.sku_id,
                "score": hit.score,
            }
            for hit in result.hits
        ],
        "filtered_out": [
            {"product_id": c.product_id, "reasons": [r.code for r in c.reasons]}
            for c in result.filtered_out
        ],
        "recall_strategy": result.recall_strategy,
        "empty_reason": result.empty_reason,
        "total": result.total,
    }


@dataclass(frozen=True)
class FixedSearchConstraints:
    """Constraints the model cannot relax, pinned by whoever built the tool.

    S9 sub-agents get their task's category/budget/currency/weight/exclusions
    fixed here. A model argument only fills a field that is ``None`` — it can
    ask for a smaller budget, never a larger one (S8-8's lesson at S9 scope).
    """

    category: str | None = None
    price_max_major: float | None = None
    currency: str = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: tuple[str, ...] = ()


def _session_id(state: AgentState | None) -> str | None:
    """The shopping session this tool call belongs to, if one is injected."""
    identity = state.middle_context.get(IDENTITY_STATE_KEY) if state is not None else None
    if not isinstance(identity, dict):
        return None
    session = identity.get("shopping_session_id")
    return session if isinstance(session, str) else None


async def _record_search_evidence(
    recorder: EvidenceRecorder, state: AgentState, hits: list[dict]
) -> None:
    """Persist hits for the session; any failure is logged, never raised.

    Evidence is an optimisation for later turns — the draft tool can still fall
    back to this turn's context. Failing the search because the evidence table
    is unavailable would turn a degraded feature into an outage (S10).
    """
    identity = state.middle_context.get(IDENTITY_STATE_KEY)
    if not isinstance(identity, dict):
        return
    buyer_id = identity.get("buyer_id")
    shopping_session_id = identity.get("shopping_session_id")
    if not buyer_id or not shopping_session_id or not hits:
        return
    evidence = [
        SearchEvidence(
            product_id=hit["product_id"],
            sku_id=hit["selected_sku_id"],
            title=hit["title"],
            unit_price=Decimal(str(hit["price_major"])),
            currency=hit["currency"],
        )
        for hit in hits
    ]
    try:
        await recorder(
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
            evidence=evidence,
        )
    except Exception:  # noqa: BLE001 — evidence is best-effort
        logger.warning("failed to record product evidence", exc_info=True)


def make_product_search_tool(
    service,
    memory=None,
    *,
    fixed_constraints: FixedSearchConstraints | None = None,
    description: str | None = None,
    evidence_recorder: EvidenceRecorder | None = None,
) -> FunctionTool:
    """Build the ``product_search`` tool backed by a ``CatalogSearchService``.

    ``memory`` (a ``PreferenceMemory``) makes the user's material dislikes a
    *hard* filter: they are merged into ``excluded_material_tags`` here, before
    the spec reaches the search service, so the exclusion holds even when the
    model forgets to pass them (S8-8).

    ``fixed_constraints`` pins a subset of the constraints for one task; set
    fields win over the model's arguments (S9).
    """
    fixed = fixed_constraints or FixedSearchConstraints()

    async def product_search(
        query: str,
        category: str | None = None,
        price_max_major: float | None = None,
        currency: str = "CNY",
        weight_max_kg: float | None = None,
        excluded_material_tags: list[str] | None = None,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        if fixed_constraints is not None:
            category = fixed.category if fixed.category is not None else category
            if fixed.price_max_major is not None:
                price_max_major = fixed.price_max_major
            currency = fixed.currency
            if fixed.weight_max_kg is not None:
                weight_max_kg = fixed.weight_max_kg

        excluded = list(fixed.excluded_material_tags)
        excluded.extend(tag for tag in (excluded_material_tags or []) if tag not in excluded)
        if memory is not None and _agent_state is not None:
            # Union, not replace: a material the user asked to skip this turn and
            # one remembered from months ago are both exclusions.
            for tag in memory.blacklisted_material_tags(_agent_state):
                if tag not in excluded:
                    excluded.append(tag)
        spec = ProductSearchSpec(
            normalized_query=query,
            price_max_major=(
                Decimal(str(price_max_major)) if price_max_major is not None else None
            ),
            currency=currency,
            category=category,
            weight_max_kg=weight_max_kg,
            excluded_material_tags=excluded,
        )
        # ``service.search`` is synchronous (Qdrant + embedding HTTP), so offload
        # it to a worker thread — otherwise it blocks the event loop and two
        # concurrent sessions serialize on it (S5-4). The span wraps the await
        # so it measures retrieval wall time, including the thread hop (S13).
        with span(
            "tool.product_search",
            query_chars=len(query),
            category=category,
            currency=currency,
        ) as search_span:
            result = await asyncio.to_thread(service.search, query, spec, 10)
            payload = _search_result_to_dict(result, currency)
            search_span.attributes.update(
                hits=len(result.hits),
                total=result.total,
                recall_strategy=result.recall_strategy,
            )
            incr("tool.search.calls")
            if not result.hits:
                incr("tool.search.empty")
            publish_event(
                "tool.search.completed",
                session_id=_session_id(_agent_state),
                hits=len(result.hits),
                total=result.total,
                recall_strategy=result.recall_strategy,
                latency_ms=result.diagnostics.latency_ms,
            )
        if evidence_recorder is not None and _agent_state is not None:
            await _record_search_evidence(evidence_recorder, _agent_state, payload["hits"])
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return FunctionTool(
        product_search,
        name="product_search",
        description=description
        or (
            "在商品目录里检索满足条件的商品。query 是自然语言描述，其余是可选的硬约束"
            "（类目、预算、币种、重量、排除材质）。用户长期不喜欢的材质会自动加入排除，"
            "无需你重复填写。返回命中商品（含最低价 SKU）以及被硬约束过滤掉的商品与原因。"
        ),
        input_schema=ProductSearchArgs,
        is_state_injected=True,
    )
