"""S4: the product_search tool wraps S3 search into a model-callable function."""

import asyncio
import json
import math
import threading
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import jsonschema
import pytest
from agentscope.state import AgentState

from harbor.agents.cards import ProductCard, ProductCardList
from harbor.agents.tools import (
    FixedSearchConstraints,
    ProductSearchArgs,
    make_product_search_tool,
)
from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.catalog.search.service import CatalogSearchService


class FakeEmbedder:
    """Deterministic character-frequency embedder; no network, no model."""

    def __init__(self, dim: int = 32) -> None:
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


def _product(pid: str, title: str, category: str = "家用电器", price: float = 100.0) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=f"CAN-{pid}",
        external_product_id=f"ext-{pid}",
        title=title,
        brand="TestBrand",
        category=category,
        tax_category=category,
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
                price=Money.from_major_units(price, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_service_bootstraps_and_searches() -> None:
    service = CatalogSearchService(
        [_product("P1", "无线吸尘器"), _product("P2", "台灯")], FakeEmbedder()
    )

    result = service.search(
        "吸尘器", ProductSearchSpec(category="家用电器", currency="CNY"), limit=10
    )

    assert result.hits
    assert {h.product.product_id for h in result.hits} >= {"P1"}


def test_tool_returns_model_readable_json() -> None:
    service = CatalogSearchService([_product("P1", "无线吸尘器 轻便", price=100.0)], FakeEmbedder())
    tool = make_product_search_tool(service)

    chunk = asyncio.run(  # type: ignore[attr-defined]
        tool._func(query="吸尘器", category="家用电器", price_max_major=500.0)
    )

    payload = json.loads(chunk.content[0].text)
    assert payload["recall_strategy"] in ("embedding", "keyword_2gram")
    assert payload["hits"][0]["title"] == "无线吸尘器 轻便"
    assert payload["hits"][0]["price_major"] == 100.0
    assert payload["hits"][0]["selected_sku_id"] == "P1-S1"


def test_tool_offloads_search_off_the_event_loop() -> None:
    """S5-4: the sync search runs in a worker thread, not the event loop.

    If ``service.search`` ran on the loop thread it would block every concurrent
    request; the tool must hand it to a worker thread so two sessions can search
    in parallel.
    """
    service = CatalogSearchService([_product("P1", "无线吸尘器")], FakeEmbedder())
    tool = make_product_search_tool(service)

    recorded: list[int] = []
    original_search = service.search

    def wrapped(query_text, spec, limit=10):
        recorded.append(threading.get_ident())
        return original_search(query_text, spec, limit)

    service.search = wrapped  # type: ignore[method-assign]

    asyncio.run(tool._func(query="吸尘器"))  # type: ignore[attr-defined]

    main_tid = threading.get_ident()
    assert recorded and recorded[0] != main_tid


def test_tool_arg_schema_has_expected_fields() -> None:
    fields = set(ProductSearchArgs.model_fields)
    assert {"query", "category", "price_max_major", "currency", "weight_max_kg"} <= fields


def test_tool_reports_total_hits_before_limit() -> None:
    """S4-3: total is the post-dedupe count before limit truncation."""
    products = [
        _product("P1", "无线吸尘器 轻便"),
        _product("P2", "无线吸尘器 大吸力"),
        _product("P3", "无线吸尘器 静音"),
    ]
    service = CatalogSearchService(products, FakeEmbedder())

    result = service.search("吸尘器", ProductSearchSpec(currency="CNY"), limit=2)

    assert result.total == 3
    assert len(result.hits) == 2


def test_tool_schema_rejects_wrong_typed_arg() -> None:
    """S4-5: a wrongly-typed argument is caught by the JSON schema, not a crash."""
    service = CatalogSearchService([_product("P1", "无线吸尘器")], FakeEmbedder())
    tool = make_product_search_tool(service)

    # price_max_major must be a number; a CJK string must fail schema validation.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"query": "吸尘器", "price_max_major": "五"},
            tool.input_schema,  # type: ignore[attr-defined]
        )


def test_tool_converts_price_to_target_currency() -> None:
    """S4-7: a foreign-currency SKU is reported in the target currency.

    The catalog is multi-currency; the tool must hand the model one stable
    currency (the spec's), not the raw SKU currency, or the model flips between
    USD and CNY run to run.
    """
    product = _product("P1", "猫饮水器")
    product.skus[0].price = Money.from_major_units(24.93, "USD")
    service = CatalogSearchService([product], FakeEmbedder())
    tool = make_product_search_tool(service)

    chunk = asyncio.run(tool._func(query="饮水器", currency="CNY"))  # type: ignore[attr-defined]
    payload = json.loads(chunk.content[0].text)

    hit = payload["hits"][0]
    assert hit["currency"] == "CNY"
    assert math.isclose(hit["price_major"], 24.93 * 7.2, rel_tol=1e-9)


class _RecordingService:
    def __init__(self) -> None:
        self.specs: list[ProductSearchSpec] = []

    def search(self, query, spec, limit=10):
        self.specs.append(spec)
        return SearchResult(hits=[], total=0)


class _FakeMemory:
    def blacklisted_material_tags(self, _state):
        return ("玻璃",)


def test_fixed_constraints_override_model_arguments() -> None:
    service = _RecordingService()
    tool = make_product_search_tool(
        service,
        memory=_FakeMemory(),
        fixed_constraints=FixedSearchConstraints(
            category="母婴宠物",
            price_max_major=500.0,
            currency="CNY",
            weight_max_kg=1.0,
            excluded_material_tags=("塑料",),
        ),
    )

    asyncio.run(
        tool.call(
            _agent_state=AgentState(),
            query="饮水器",
            category="家用电器",
            price_max_major=9999.0,
            currency="USD",
            weight_max_kg=5.0,
            excluded_material_tags=["金属"],
        )
    )

    spec = service.specs[0]
    assert spec.category == "母婴宠物"
    assert spec.price_max_major == Decimal("500.0")
    assert spec.currency == "CNY"
    assert spec.weight_max_kg == 1.0
    assert set(spec.excluded_material_tags) == {"塑料", "玻璃", "金属"}


def test_unspecified_fixed_fields_fall_back_to_model_arguments() -> None:
    service = _RecordingService()
    tool = make_product_search_tool(
        service, fixed_constraints=FixedSearchConstraints(currency="CNY")
    )

    asyncio.run(tool.call(_agent_state=AgentState(), query="杯子", category="家居生活"))

    assert service.specs[0].category == "家居生活"


def test_card_schema_roundtrip() -> None:
    card = ProductCard(
        title="无线吸尘器",
        brand="TestBrand",
        category="家用电器",
        price_major=100.0,
        currency="CNY",
        weight_kg=0.5,
        sku_id="P1-S1",
    )
    cards = ProductCardList(cards=[card], summary="1 件命中")

    assert cards.cards[0].sku_id == "P1-S1"
    assert cards.summary == "1 件命中"
