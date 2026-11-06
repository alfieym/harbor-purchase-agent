"""S10: product_search records its hits as durable evidence, best-effort."""

import asyncio
from datetime import date

from agentscope.message import ToolResultState
from agentscope.state import AgentState

from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.agents.tools import make_product_search_tool
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult


def _product(pid: str, title: str) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=title,
        brand="TestBrand",
        category="家用电器",
        tax_category="家用电器",
        description="",
        highlights=[{"label": "卖点", "detail": "轻便"}],
        material_tags=["ABS塑料"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=0.5,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{pid}-S1",
                spec="标准",
                price=Money.from_major_units(89.0, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


class _OneHitService:
    def __init__(self, product: Product) -> None:
        self.product = product

    def search(self, query, spec, limit=10):
        return SearchResult(
            hits=[SearchHit(product=self.product, score=1.0, selected_sku=self.product.skus[0])],
            total=1,
        )


def _identity_state() -> AgentState:
    state = AgentState()
    state.middle_context[IDENTITY_STATE_KEY] = {"buyer_id": "b1", "shopping_session_id": "s1"}
    return state


def test_search_records_hits_for_the_session() -> None:
    calls: list[dict] = []

    async def recorder(**kwargs):
        calls.append(kwargs)

    tool = make_product_search_tool(
        _OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder
    )

    asyncio.run(tool.call(_agent_state=_identity_state(), query="饮水器"))

    assert calls[0]["buyer_id"] == "b1"
    assert calls[0]["shopping_session_id"] == "s1"
    assert calls[0]["evidence"][0].sku_id == "P1-S1"
    assert calls[0]["evidence"][0].currency == "CNY"
    assert calls[0]["evidence"][0].unit_price == 89


def test_search_without_identity_does_not_record() -> None:
    calls: list[dict] = []

    async def recorder(**kwargs):
        calls.append(kwargs)

    tool = make_product_search_tool(
        _OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder
    )
    asyncio.run(tool.call(_agent_state=AgentState(), query="饮水器"))
    assert calls == []


def test_evidence_recording_failure_does_not_fail_search() -> None:
    async def recorder(**kwargs):
        raise RuntimeError("disk full")

    tool = make_product_search_tool(
        _OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder
    )

    chunk = asyncio.run(tool.call(_agent_state=_identity_state(), query="饮水器"))

    assert chunk.state == ToolResultState.SUCCESS
