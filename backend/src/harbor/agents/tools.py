"""The ``product_search`` AgentScope tool wrapping the S3 search (S4).

The tool is a thin adapter: the model supplies ``ProductSearchArgs``, this module
builds a ``ProductSearchSpec``, calls ``CatalogSearchService.search``, and returns
the ``SearchResult`` as a compact JSON chunk the model can read.
"""

import asyncio
import json
from decimal import Decimal

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk
from pydantic import BaseModel, Field

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.search.fx import convert
from harbor.features.catalog.search.schemas import SearchResult


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


def make_product_search_tool(service) -> FunctionTool:
    """Build the ``product_search`` tool backed by a ``CatalogSearchService``."""

    async def product_search(
        query: str,
        category: str | None = None,
        price_max_major: float | None = None,
        currency: str = "CNY",
        weight_max_kg: float | None = None,
        excluded_material_tags: list[str] | None = None,
    ) -> ToolChunk:
        spec = ProductSearchSpec(
            normalized_query=query,
            price_max_major=(
                Decimal(str(price_max_major)) if price_max_major is not None else None
            ),
            currency=currency,
            category=category,
            weight_max_kg=weight_max_kg,
            excluded_material_tags=list(excluded_material_tags or []),
        )
        # ``service.search`` is synchronous (Qdrant + embedding HTTP), so offload
        # it to a worker thread — otherwise it blocks the event loop and two
        # concurrent sessions serialize on it (S5-4).
        result = await asyncio.to_thread(service.search, query, spec, 10)
        return ToolChunk(
            content=[
                TextBlock(
                    text=json.dumps(_search_result_to_dict(result, currency), ensure_ascii=False)
                )
            ],
            state=ToolResultState.SUCCESS,
        )

    return FunctionTool(
        product_search,
        name="product_search",
        description=(
            "在商品目录里检索满足条件的商品。query 是自然语言描述，其余是可选的硬约束"
            "（类目、预算、币种、重量、排除材质）。返回命中商品（含最低价 SKU）以及"
            "被硬约束过滤掉的商品与原因。"
        ),
        input_schema=ProductSearchArgs,
    )
