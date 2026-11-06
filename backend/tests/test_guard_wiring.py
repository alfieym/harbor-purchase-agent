"""S11-18/19: assembly wires the guard in, and the kill switch removes it."""

import json
from collections.abc import Sequence
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolBase, ToolChunk
from qdrant_client import QdrantClient

import harbor.agents.factory as factory
from harbor.agents.dispatch import AgentSubAgentRunner
from harbor.agents.factory import AgentRuntime, build_runtime
from harbor.core.config import Settings
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.tools import GuardedTool, wrap_tools

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class EchoToolStub(ToolBase):
    name = "echo"
    description = "echo"
    input_schema = {"type": "object"}
    is_concurrency_safe = True
    is_read_only = True

    def __init__(self, name: str = "echo") -> None:
        super().__init__(middlewares=None)
        self.name = name

    async def check_permissions(self, *args, **kwargs):
        return True

    async def call(self, **kwargs) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True}, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )


def _offline_runtime(monkeypatch, tmp_path, **settings_kwargs) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        memory_db_path=tmp_path / "memory.sqlite3",
        guard_budget_db_path=tmp_path / "b.sqlite3",
        **settings_kwargs,
    )
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _settings: FakeEmbedder())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _model: None)
    monkeypatch.setattr(
        factory, "_open_index", lambda *_args, **_kwargs: (QdrantClient(":memory:"), "fp-123")
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    return build_runtime(settings, data_path=FIXTURE)


def test_kill_switch_leaves_model_and_tools_untouched(monkeypatch, tmp_path) -> None:
    rt = _offline_runtime(monkeypatch, tmp_path, guardrails_enabled=False)
    assert rt.guard is None
    assert not isinstance(rt.model, GuardedChatModel)


def test_enabled_wraps_model_once(monkeypatch, tmp_path) -> None:
    rt = _offline_runtime(monkeypatch, tmp_path)
    assert isinstance(rt.model, GuardedChatModel)
    assert rt.guard is not None


def test_wrap_tools_returns_guarded_proxies(monkeypatch, tmp_path) -> None:
    rt = _offline_runtime(monkeypatch, tmp_path)
    assert rt.guard is not None
    tools = [EchoToolStub(), EchoToolStub()]
    wrapped = wrap_tools(
        tools,
        session=rt.guard.new_session(),
        metrics=rt.guard.metrics,
        max_chars=8000,
    )
    assert all(isinstance(tool, GuardedTool) for tool in wrapped)
    assert len(wrapped) == 2


def test_wrap_tools_is_a_noop_without_guard(monkeypatch, tmp_path) -> None:
    raw = [EchoToolStub()]
    assert wrap_tools(raw, session=None, metrics=None, max_chars=0) is raw


def test_sub_agents_get_independent_sessions(monkeypatch, tmp_path) -> None:
    # S11-19: a sub-agent's loop window must not leak into the parent or siblings.
    rt = _offline_runtime(monkeypatch, tmp_path)
    runner = AgentSubAgentRunner(
        model=rt.model, service=rt.service, categories=rt.categories, guard=rt.guard
    )
    tool = EchoToolStub(name="product_search")
    a = runner._build_agent("a", "p", tool)
    b = runner._build_agent("b", "p", tool)
    assert a.guard_session is not None
    assert a.guard_session is not b.guard_session
    a.guard_session.observe("product_search", {"query": "x"})
    assert len(b.guard_session.recent_tool_calls) == 0
