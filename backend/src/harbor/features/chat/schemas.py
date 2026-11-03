"""Commerce intents request/response schemas (S5) + session listing (S6).

The contract matches the acceptance checklists: ``POST /commerce/intents`` takes
``buyer_id`` + ``shopping_session_id`` + ``raw_query`` and answers with
``final_text`` + the product cards; ``GET /commerce/sessions`` lists one buyer's
sessions newest-first by ``last_active_at``.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from harbor.agents.cards import ProductCard


class CommerceIntentRequest(BaseModel):
    buyer_id: str = Field(description="买家标识，用于多租户会话隔离")
    shopping_session_id: str = Field(description="购物会话标识；同一买家+会话复用上下文")
    raw_query: str = Field(description="用户原始问询，例如「2000 以内的吸尘器」")


class CommerceIntentResponse(BaseModel):
    final_text: str = Field(description="一句话结论（命中数量 / 拒绝原因）")
    cards: list[ProductCard] = Field(description="推荐的商品卡列表")


class SessionSummary(BaseModel):
    """One row of the buyer's session list (S6-5)."""

    shopping_session_id: str = Field(description="购物会话标识")
    last_active_at: datetime = Field(description="最后一次成功持久化的时间")
    turn_count: int = Field(description="已完成问答轮数（state.context 里 user 消息数）")


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary] = Field(description="该买家的会话，按 last_active_at 倒序")
