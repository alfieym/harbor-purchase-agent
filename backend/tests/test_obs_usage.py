"""S13-3: token usage is recorded, never estimated."""

import asyncio

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.runtime import GuardRuntime
from harbor.observability import Observability, configure_observability
from harbor.observability.usage import UNKNOWN, record_usage


@pytest.fixture()
def obs():
    instance = Observability.create()
    previous = configure_observability(instance)
    yield instance
    configure_observability(previous)


def _response(usage: ChatUsage | None) -> ChatResponse:
    return ChatResponse(content=[TextBlock(text="ok")], is_last=True, usage=usage)


def _messages():
    return [Msg(name="user", role="user", content=[TextBlock(text="hi")])]


def _guard(tmp_path) -> GuardRuntime:
    metrics = GuardMetrics()
    return GuardRuntime(
        budget=SqliteTokenBudget(tmp_path / "budget.sqlite3", 1000),
        circuits=CircuitBreaker(3, 60.0, clock=lambda: 0.0, metrics=metrics),
        metrics=metrics,
    )


def test_reported_usage_is_recorded_as_reported(obs) -> None:
    usage = ChatUsage(
        input_tokens=120,
        output_tokens=30,
        time=0.5,
        cache_input_tokens=80,
        cache_creation_input_tokens=0,
    )

    fields = record_usage(_response(usage))

    assert fields == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cache_read_tokens": 80,
        # AgentScope defaults cache creation to 0 when the provider does not
        # report it, so a zero cannot be passed off as a measured value.
        "cache_write_tokens": UNKNOWN,
    }
    counters = obs.metrics.snapshot()["counters"]
    assert counters["model.usage.prompt_tokens"] == 120
    assert counters["model.usage.completion_tokens"] == 30
    assert counters["model.usage.cache_read_tokens"] == 80
    assert "model.usage.cache_write_tokens" not in counters


def test_missing_usage_is_unknown_and_not_estimated(obs) -> None:
    fields = record_usage(_response(None))

    assert fields == {
        "prompt_tokens": UNKNOWN,
        "completion_tokens": UNKNOWN,
        "cache_read_tokens": UNKNOWN,
        "cache_write_tokens": UNKNOWN,
    }
    counters = obs.metrics.snapshot()["counters"]
    assert counters["model.usage.missing"] == 1
    assert "model.usage.prompt_tokens" not in counters
    assert "model.usage.completion_tokens" not in counters


class _UsageModel(ChatModelBase):
    def __init__(self, usage: ChatUsage | None) -> None:
        super().__init__(
            credential=None, model="m", parameters=None, stream=False, max_retries=0
        )
        self._usage = usage

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        return _response(self._usage)


def test_model_call_span_carries_usage(obs, tmp_path) -> None:
    usage = ChatUsage(input_tokens=10, output_tokens=4, time=0.1)
    guarded = GuardedChatModel(
        _UsageModel(usage), _guard(tmp_path), request_timeout=1, max_retries=0, backoff=0.0
    )

    asyncio.run(guarded(_messages()))

    span = obs.traces.recent()[0].spans[0]
    assert span.name == "model.call"
    assert span.attributes["prompt_tokens"] == 10
    assert span.attributes["completion_tokens"] == 4
    assert span.attributes["cache_read_tokens"] == UNKNOWN
    assert span.attributes["cache_write_tokens"] == UNKNOWN
