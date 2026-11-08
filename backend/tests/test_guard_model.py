import asyncio
import inspect
from pathlib import Path

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.model import GuardedChatModel
from harbor.guardrails.runtime import GuardRuntime


class FakeModel(ChatModelBase):
    def __init__(self, model: str, script):
        # ``ChatModelBase`` defaults to ``stream=True``; this fake answers with
        # one complete response, so it declares itself honestly.
        super().__init__(
            credential=None, model=model, parameters=None, stream=False, max_retries=0
        )
        self.script = list(script)
        self.calls = 0

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResponse(content=[TextBlock(text=item)], is_last=True)


class FakeStreamingModel(ChatModelBase):
    """A delegate that answers as an async generator, like a real provider.

    ``first_call_error`` fails the first call before it yields anything (a
    retryable setup failure); ``boom_at`` fails *after* that many chunks have
    been handed over (a failure no retry can repair).
    """

    def __init__(
        self,
        model: str,
        chunks: list[str],
        *,
        first_call_error: Exception | None = None,
        boom_at: int | None = None,
        delay: float = 0.0,
    ):
        super().__init__(
            credential=None, model=model, parameters=None, stream=True, max_retries=0
        )
        self.chunks = chunks
        self.first_call_error = first_call_error
        self.boom_at = boom_at
        self.delay = delay
        self.calls = 0

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        first = self.calls == 1

        async def stream():
            if first and self.first_call_error is not None:
                raise self.first_call_error
            for index, text in enumerate(self.chunks):
                if self.boom_at is not None and index == self.boom_at:
                    raise ConnectionError("dropped mid-stream")
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield ChatResponse(content=[TextBlock(text=text)], is_last=False)
            yield ChatResponse(
                content=[],
                is_last=True,
                usage=ChatUsage(input_tokens=3, output_tokens=5, time=0.1),
            )

        return stream()


async def _streamed_text(guarded: GuardedChatModel) -> list[str]:
    response = await guarded(_msgs())
    assert inspect.isasyncgen(response), "a streaming delegate must stay streaming"
    texts: list[str] = []
    async for chunk in response:
        texts.append(
            "".join(
                block.text
                for block in chunk.content
                if isinstance(getattr(block, "text", None), str)
            )
        )
    return texts


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


class _Formatter:
    supported_input_media_types = ("image/*", "audio/*", "video/*")


def test_guarded_model_exposes_the_delegate_formatter(tmp_path) -> None:
    # AgentScope's Agent reads ``self.model.formatter`` to decide which input
    # media to accept. The wrapper replaces the provider model, so it has to
    # pass the provider's formatter through or every real turn crashes.
    delegate = FakeModel("m", ["ok"])
    delegate.formatter = _Formatter()

    guarded = GuardedChatModel(
        delegate, _runtime(tmp_path), request_timeout=1, max_retries=0, backoff=0.0
    )

    assert guarded.formatter is delegate.formatter


# --------------------------------------------------------------- streaming (S14)


def test_a_streaming_delegate_is_passed_through_chunk_by_chunk(tmp_path) -> None:
    # S14-1: the guard sits between the agent and the provider. If it turns a
    # stream into one buffered response, no token can ever reach the screen.
    m = FakeStreamingModel("m", ["你", "好"])
    g = GuardedChatModel(m, _runtime(tmp_path), request_timeout=1, max_retries=0, backoff=0.0)

    assert asyncio.run(_streamed_text(g)) == ["你", "好", ""]
    assert m.calls == 1


def test_a_stream_that_fails_before_its_first_chunk_is_retried(tmp_path) -> None:
    # Nothing has reached the consumer yet, so retrying is still honest.
    m = FakeStreamingModel("m", ["ok"], first_call_error=ConnectionError("connect refused"))
    g = GuardedChatModel(m, _runtime(tmp_path), request_timeout=1, max_retries=1, backoff=0.0)

    assert asyncio.run(_streamed_text(g)) == ["ok", ""]
    assert m.calls == 2
    assert g.runtime.metrics.snapshot()["model.retry"] == 1


def test_a_stream_that_breaks_after_yielding_is_not_retried(tmp_path) -> None:
    # Retrying here would replay tokens the client already rendered: a
    # duplicated answer is worse than a failed turn.
    m = FakeStreamingModel("m", ["a", "b"], boom_at=1)
    g = GuardedChatModel(m, _runtime(tmp_path), request_timeout=1, max_retries=2, backoff=0.0)

    with pytest.raises(ConnectionError):
        asyncio.run(_streamed_text(g))
    assert m.calls == 1


def test_the_deadline_bounds_consumption_not_only_setup(tmp_path) -> None:
    # The old design forced ``stream=False`` because ``wait_for`` around an
    # async generator returns at setup. A real stream needs a deadline that
    # spans the chunks, or one stalled connection holds the turn forever.
    m = FakeStreamingModel("m", ["a", "b", "c", "d"], delay=0.2)
    g = GuardedChatModel(m, _runtime(tmp_path), request_timeout=0.05, max_retries=0, backoff=0.0)

    with pytest.raises(TimeoutError):
        asyncio.run(_streamed_text(g))


def test_the_budget_is_settled_from_the_last_chunk(tmp_path) -> None:
    m = FakeStreamingModel("m", ["a", "b"])
    budget = SqliteTokenBudget(tmp_path / "b.sqlite3", 1000)
    rt = GuardRuntime(
        budget=budget,
        circuits=CircuitBreaker(threshold=2, cooldown_seconds=60, clock=lambda: 0.0),
        metrics=GuardMetrics(),
        lite_model=None,
        fallback_model=None,
    )
    g = GuardedChatModel(m, rt, request_timeout=1, max_retries=0, backoff=0.0)

    asyncio.run(_streamed_text(g))

    # usage says 3 in + 5 out, so the estimate must have been replaced by it.
    assert budget.used_today() == 8


def test_a_retried_stream_keeps_its_reservation_and_settles_once(tmp_path) -> None:
    # The retry happens before anything was billed, so it must *reuse* the
    # estimate that was already reserved. Releasing on the failed attempt and
    # settling on the good one would leave the day's spend short by the
    # estimate — invisible until the budget is nearly gone.
    m = FakeStreamingModel("m", ["ok"], first_call_error=ConnectionError("connect refused"))
    budget = SqliteTokenBudget(tmp_path / "b.sqlite3", 1000)
    rt = GuardRuntime(
        budget=budget,
        circuits=CircuitBreaker(threshold=2, cooldown_seconds=60, clock=lambda: 0.0),
        metrics=GuardMetrics(),
        lite_model=None,
        fallback_model=None,
    )
    g = GuardedChatModel(m, rt, request_timeout=1, max_retries=1, backoff=0.0)

    asyncio.run(_streamed_text(g))

    assert m.calls == 2
    assert budget.used_today() == 8

