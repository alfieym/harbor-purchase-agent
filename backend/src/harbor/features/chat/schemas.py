"""Commerce intents request/response schemas (S5).

The contract matches the S5 acceptance checklist: ``POST /commerce/intents``
takes ``buyer_id`` + ``shopping_session_id`` + ``raw_query`` and answers with
``final_text`` + the product cards.
"""

from pydantic import BaseModel, Field

from harbor.agents.cards import ProductCard


class CommerceIntentRequest(BaseModel):
    buyer_id: str = Field(description="买家标识，用于多租户会话隔离")
    shopping_session_id: str = Field(description="购物会话标识；同一买家+会话复用上下文")
    raw_query: str = Field(description="用户原始问询，例如「2000 以内的吸尘器」")


class CommerceIntentResponse(BaseModel):
    final_text: str = Field(description="一句话结论（命中数量 / 拒绝原因）")
    cards: list[ProductCard] = Field(description="推荐的商品卡列表")
