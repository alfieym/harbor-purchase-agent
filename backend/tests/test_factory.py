"""S5: the shared factory assembles a runtime + per-session agent (no network)."""

import asyncio
from collections.abc import Sequence
from pathlib import Path

from agentscope.agent import Agent
from qdrant_client import QdrantClient

import harbor.agents.factory as factory
from harbor.agents.context_governance import SessionArchiveOffloader
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


def test_build_agent_wires_s7_governance(monkeypatch, tmp_path) -> None:
    """The factory arms the agent with the archive tool, offloader and gate."""
    settings = Settings(_env_file=None, openai_api_key="sk-test")
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-123")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    runtime = build_runtime(settings, data_path=FIXTURE)

    agent = build_agent(runtime)

    names = {tool.name for tool in agent.toolkit.tool_groups[0].tools}
    assert names == {"product_search", "read_archive"}
    assert isinstance(agent.offloader, SessionArchiveOffloader)
    # Truncation would drop facts silently; S7 turns it off and rolls back instead.
    assert agent.context_config.compression_fallback_to_truncation is False
    assert agent.context_config.trigger_ratio == settings.context_trigger_ratio


def test_build_agent_legacy_strategy_is_the_pre_s7_baseline(monkeypatch, tmp_path) -> None:
    """S7-5: ``legacy`` removes the governance layer and its archive tool."""
    settings = Settings(_env_file=None, openai_api_key="sk-test", context_strategy="legacy")
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-123")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    runtime = build_runtime(settings, data_path=FIXTURE)

    agent = build_agent(runtime)

    names = {tool.name for tool in agent.toolkit.tool_groups[0].tools}
    assert names == {"product_search"}
    assert agent.offloader is None
    assert agent._reply_middlewares == []
    # AgentScope's own defaults are the baseline S7 replaces.
    assert agent.context_config.trigger_ratio == 0.8
    assert agent.context_config.compression_fallback_to_truncation is True
    assert "read_archive" not in asyncio.run(agent._get_system_prompt())
