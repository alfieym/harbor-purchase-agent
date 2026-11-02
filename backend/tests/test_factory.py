"""S5: the shared factory assembles a runtime + per-session agent (no network)."""

from collections.abc import Sequence
from pathlib import Path

from agentscope.agent import Agent
from qdrant_client import QdrantClient

import harbor.agents.factory as factory
from harbor.agents.factory import AgentRuntime, build_agent, build_runtime
from harbor.core.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


def test_build_runtime_and_agent_wire_shared_pieces(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, openai_api_key="sk-test")

    # Offline assembly: fake embedder, disabled reranker, in-memory index, and a
    # scratch fingerprint path so nothing touches the network or the repo cache.
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-123")
    )
    fingerprint = tmp_path / "fingerprint.txt"
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", fingerprint)

    runtime = build_runtime(settings, data_path=FIXTURE)

    assert isinstance(runtime, AgentRuntime)
    assert runtime.products
    # Categories are deduped and sorted so the system prompt carries the real
    # catalog vocabulary (no duplicates, stable order).
    assert runtime.categories == ["家用电器", "母婴宠物"]
    # The fingerprint is persisted once the index is ready, so the next process
    # reuses the on-disk index instead of re-embedding the catalog.
    assert fingerprint.read_text() == "fp-123"

    agent = build_agent(runtime)
    assert isinstance(agent, Agent)
    # The agent carries a toolkit (the product_search tool itself is covered by
    # test_product_search_tool.py; here we verify the full wiring assembles).
    assert agent.toolkit is not None
