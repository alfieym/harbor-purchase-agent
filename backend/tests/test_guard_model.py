import asyncio
from pathlib import Path

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.runtime import GuardRuntime


class FakeModel(ChatModelBase):
    def __init__(self, model: str, script):
        super().__init__(credential=None, model=model, parameters=None, max_retries=0)
        self.script = list(script)
        self.calls = 0

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResponse(content=[TextBlock(text=item)], is_last=True)


def _runtime(tmp_path: Path, cap: int = 1000, *, lite=None, fallback=None) -> GuardRuntime:
    return GuardRuntime(
        budget=SqliteTokenBudget(tmp_path / "b.sqlite3", cap),
        circuits=CircuitBreaker(threshold=2, cooldown_seconds=60, clock=lambda: 0.0),
        metrics=GuardMetrics(),
        lite_model=lite,
        fallback_model=fallback,
    )


def _msgs():
    return [Msg(name="user", role="user", content=[TextBlock(text="hi")])]


def _text(response: ChatResponse) -> str:
    return "".join(
        block.text
        for block in response.content
        if isinstance(getattr(block, "text", None), str)
    )


def test_timeout_is_retried(tmp_path) -> None:
    slow = FakeModel("m", [asyncio.TimeoutError(), "ok"])
    g = GuardedChatModel(slow, _runtime(tmp_path), request_timeout=0.01, max_retries=1, backoff=0.0)
    res = asyncio.run(g(_msgs()))
    assert _text(res) == "ok"
    assert slow.calls == 2
    assert g.runtime.metrics.snapshot()["model.retry"] == 1


def test_non_retryable_error_is_raised_immediately(tmp_path) -> None:
    class BadRequest(Exception):
        status_code = 400

    m = FakeModel("m", [BadRequest()])
    g = GuardedChatModel(m, _runtime(tmp_path), request_timeout=1, max_retries=2, backoff=0.0)
    with pytest.raises(BadRequest):
        asyncio.run(g(_msgs()))
    assert m.calls == 1


def test_retry_exhausted_falls_back(tmp_path) -> None:
    class Boom(Exception):
        status_code = 500

    primary = FakeModel("main", [Boom(), Boom(), Boom()])
    backup = FakeModel("backup", ["from-backup"])
    rt = _runtime(tmp_path, fallback="backup")
    g = GuardedChatModel(primary, rt, request_timeout=1, max_retries=0, backoff=0.0)
    g._model_factory = lambda name: backup if name == "backup" else primary
    assert _text(asyncio.run(g(_msgs()))) == "from-backup"
    assert g.runtime.metrics.snapshot()["model.fallback"] >= 1


def test_budget_tiers_select_model(tmp_path) -> None:
    rt = _runtime(tmp_path, cap=1000, lite="lite-1")
    rt.budget.acquire(700); rt.budget.settle(700, 700)
    primary = FakeModel("main", ["a"])
    lite = FakeModel("lite-1", ["b"])
    g = GuardedChatModel(primary, rt, request_timeout=1, max_retries=0, backoff=0.0)
    g._model_factory = lambda name: lite if name == "lite-1" else primary
    assert _text(asyncio.run(g(_msgs()))) == "b"
