"""Structured product-card output the agent is asked to produce (S4).

The model receives the raw ``SearchResult`` from the ``product_search`` tool and
formats it into these cards. Passed as ``structured_schema`` to ``Agent.reply`` so
the final answer is a validated ``ProductCardList``.
"""

from pydantic import BaseModel, Field


class ProductCard(BaseModel):
    """One product, reduced to the fields a buyer cares about."""

    title: str = Field(description="商品标题")
    brand: str = Field(description="品牌")
    category: str = Field(description="类目")
    price_major: float = Field(description="最低 SKU 价格（主币单位，已换算为目标币种）")
    currency: str = Field(description="币种，ISO-4217 三位码，如 CNY")
    weight_kg: float = Field(description="重量（kg）")
    material_tags: list[str] = Field(default_factory=list, description="材质标签")
    highlights: list[str] = Field(default_factory=list, description="卖点")
    sku_id: str = Field(description="推荐的最低价格 SKU 编号")
    score: float | None = Field(default=None, description="检索/精排分数，可空")


class ProductCardList(BaseModel):
    """The agent's final answer: the cards it chose to present, plus a one-line summary."""

    cards: list[ProductCard] = Field(description="要展示给用户的商品卡列表")
    summary: str = Field(description="一句话总结，例如命中数量、被过滤原因")
