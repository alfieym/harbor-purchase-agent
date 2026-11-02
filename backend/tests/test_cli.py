"""S4: the CLI assembles a working Agent + product_search tool (no network)."""

from collections.abc import Sequence
from datetime import date

from agentscope.agent import Agent

import harbor.agents.cli as cli
from harbor.core.config import Settings
from harbor.features.catalog.models import Money, Product, Sku


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


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
        highlights=[],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=0.5,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(sku_id=f"{pid}-S1", spec="标准", price=Money.from_major_units(100, "CNY"), stock=10)
        ],
        updated_at=date(2026, 8, 1),
    )


def test_build_agent_wires_tool_and_model(monkeypatch) -> None:
    settings = Settings(_env_file=None, openai_api_key="sk-test")

    # Inject a fake embedder + disabled reranker so index build makes no network call.
    monkeypatch.setattr(cli, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(cli, "FastembedReranker", lambda _model: None)

    agent = cli.build_agent(settings, [_product("P1", "吸尘器")])

    assert isinstance(agent, Agent)
    # The agent carries a toolkit (the product_search tool itself is covered by
    # test_product_search_tool.py; here we just verify the full wiring assembles).
    assert agent.toolkit is not None
