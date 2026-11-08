"""Request-layer guardrails wrapped around any ``ChatModelBase`` (spec §5).

Wrapping happens once at the factory exit, so the main agent, S9 sub-agents
and S7 summary calls all get timeout, retry, circuit, fallback and budget
accounting without touching their call sites.

The delegate's own retry is disabled: AgentScope's ``ChatModelBase`` retries
by default, and leaving it on would multiply with these retries and make the
retry counter, circuit decision and budget settlement all wrong.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import AsyncGenerator, Sequence
from typing import Any, cast

from agentscope.message import SystemMsg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from harbor.guardrails.budget import Admission, BudgetTier
from harbor.guardrails.events import log_guard_event
from harbor.guardrails.runtime import GuardRuntime
from harbor.observability import span
from harbor.observability.usage import record_usage

MINIMAL_MODE_HINT = (
    "当前处于预算紧张模式：最多再做一次必要检索，不要重复调用工具，请基于已有结果直接给出简洁回复。"
)


class BudgetExhausted(RuntimeError):
    """The daily token budget refused this call; no request was sent."""


def clone_model(delegate: ChatModelBase, name: str) -> ChatModelBase:
    """Same provider, key and base URL; only the model name changes."""
    clone = copy.copy(delegate)
    clone.model = name
    clone.max_retries = 0
    return clone


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    return status == 429 or (isinstance(status, int) and 500 <= status < 600)


class GuardedChatModel(ChatModelBase):
    def __init__(
        self,
        delegate: ChatModelBase,
        runtime: GuardRuntime,
        *,
        request_timeout: float,
        max_retries: int,
        backoff: float,
    ) -> None:
        super().__init__(
            credential=delegate.credential,
            model=delegate.model,
            parameters=delegate.parameters,
            # The shape follows the delegate: a streaming provider stays
            # streaming, and this wrapper takes over the guarantees the
            # delegate used to provide around the whole response (S14-1).
            stream=delegate.stream,
            max_retries=0,
            retry_delay=0.0,
            context_size=delegate.context_size,
        )
        self._delegate = delegate
        self._delegate.max_retries = 0
        # AgentScope's Agent reads ``model.formatter`` to decide which input
        # media it can accept. The wrapper stands in for the provider model,
        # so it must pass that provider-specific object through.
        formatter = getattr(delegate, "formatter", None)
        if formatter is not None:
            self.formatter = formatter
        self.runtime = runtime
        self.request_timeout = request_timeout
        self._max_retries = max_retries
        self._backoff = backoff
        self._model_factory = self._default_factory

    # ------------------------------------------------------------- internals

    def _default_factory(self, name: str) -> ChatModelBase:
        return self._delegate if name == self.model else clone_model(self._delegate, name)

    def _candidates(self, selected: str) -> list[str]:
        chain = [selected]
        fallback = self.runtime.fallback_model
        if self.runtime.switches.fallback and fallback and fallback != selected:
            chain.append(fallback)
        return chain

    @staticmethod
    def _prepare(messages: Sequence[Any], tier: BudgetTier) -> list[Any]:
        prepared = list(messages)
        if tier is not BudgetTier.MINIMAL:
            return prepared
        hint = SystemMsg(name="system", content=[TextBlock(text=MINIMAL_MODE_HINT)])
        index = 1 if prepared and getattr(prepared[0], "role", None) == "system" else 0
        return prepared[:index] + [hint] + prepared[index:]

    async def _settle(self, admission: Admission | None, response: ChatResponse | None) -> None:
        # ``None`` means the stream ended without a final chunk: the provider
        # reported nothing, so the estimate stands, exactly as an unreported
        # usage field would.
        if admission is None:
            return
        usage = (
            response.get("usage")
            if isinstance(response, dict)
            else getattr(response, "usage", None)
        )
        if usage is None:
            await self.runtime.budget.settle_async(admission.reserved, admission.reserved)
            return
        actual = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        await self.runtime.budget.settle_async(admission.reserved, actual)

    def _fallback_event(self, name: str, reason: str, tier: BudgetTier | None = None) -> None:
        self.runtime.metrics.incr("model.fallback")
        log_guard_event(
            "model.fallback",
            model=name,
            reason=reason,
            tier=tier.value if tier is not None else None,
        )

    def _exhausted(self, reason: str) -> BudgetExhausted:
        self.runtime.metrics.incr("budget.exhausted")
        log_guard_event("budget.exhausted", reason=reason)
        return BudgetExhausted("daily token budget exhausted")

    # ------------------------------------------------------------------ call

    async def _plan(self, messages: Sequence[Any]) -> tuple[BudgetTier, str, list[Any]]:
        """Pick the tier, the model for it, and the (possibly hinted) messages."""
        switches = self.runtime.switches
        # With the budget off the model sees the pre-S11 world: no tier, no
        # admission, no per-call estimate — just the call.
        tier = await self.runtime.budget.tier_async() if switches.budget else BudgetTier.MAIN
        if tier is BudgetTier.FALLBACK:
            # The turn-level gate should have caught this already; if it did
            # not, still refuse rather than send a doomed request.
            raise self._exhausted("tier_fallback")

        selected = self.model
        if tier in (BudgetTier.LITE, BudgetTier.MINIMAL) and self.runtime.lite_model:
            selected = self.runtime.lite_model
        if tier is not BudgetTier.MAIN:
            reason = "budget_tier_lite" if tier is BudgetTier.LITE else "budget_tier_minimal"
            self._fallback_event(selected, reason, tier)
            self.runtime.last_tier = tier

        return tier, selected, self._prepare(messages, tier)

    async def __call__(
        self,
        messages: Sequence[Any],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if self.stream:
            # A streaming turn cannot be an atomic request: the deadline has to
            # span consumption, the budget can only settle on the last chunk,
            # and a retry is honest only before the first chunk left. ``_stream``
            # owns all three; this method only hands it over.
            return self._stream(messages, tools, tool_choice, kwargs)

        switches = self.runtime.switches
        tier, selected, prepared = await self._plan(messages)
        estimate = self.runtime.budget.estimate(prepared) if switches.budget else 0
        max_retries = self._max_retries if switches.retry else 0
        timeout = self.request_timeout if switches.timeout else None
        last_error: BaseException | None = None
        skipped_for_circuit = False

        for index, name in enumerate(self._candidates(selected)):
            if switches.circuit and not self.runtime.circuits.allow(name):
                skipped_for_circuit = True
                log_guard_event("circuit.skip", model=name, reason="circuit_open")
                continue
            admission: Admission | None = None
            if switches.budget:
                admission = await self.runtime.budget.acquire_async(estimate)
                if not admission.granted:
                    raise self._exhausted("acquire_refused")
            if index > 0 or skipped_for_circuit:
                reason = "circuit_open" if skipped_for_circuit else "retry_exhausted"
                self._fallback_event(name, reason, tier)

            for attempt in range(max_retries + 1):
                try:
                    # S13: one span per provider request, so a retry is visible
                    # as two model.call spans rather than one long one.
                    with span(
                        "model.call",
                        model=name,
                        tier=tier.value,
                        attempt=attempt + 1,
                        fallback=index > 0 or skipped_for_circuit,
                    ) as call_span:
                        response = await asyncio.wait_for(
                            self._model_factory(name)(
                                prepared,
                                tools=tools,
                                tool_choice=tool_choice,
                                **kwargs,
                            ),
                            timeout=timeout,
                        )
                        # S13-3: prompt/completion/cache as the provider
                        # reported them; anything unreported stays "unknown".
                        call_span.attributes.update(record_usage(response))
                except asyncio.CancelledError:
                    if admission is not None:
                        await self.runtime.budget.release_async(admission.reserved)
                    raise
                except Exception as exc:  # noqa: BLE001 - classified below
                    if not _retryable(exc):
                        if admission is not None:
                            await self.runtime.budget.release_async(admission.reserved)
                        raise
                    if attempt < max_retries:
                        self.runtime.metrics.incr("model.retry")
                        log_guard_event("model.retry", model=name, count=attempt + 1)
                        if self._backoff:
                            await asyncio.sleep(self._backoff * (2**attempt))
                        continue
                    if admission is not None:
                        await self.runtime.budget.release_async(admission.reserved)
                    if switches.circuit:
                        self.runtime.circuits.record_failure(name)
                    last_error = exc
                    break
                else:
                    await self._settle(admission, response)
                    if switches.circuit:
                        self.runtime.circuits.record_success(name)
                    return response

        if last_error is not None:
            raise last_error
        raise self._exhausted("no_candidate")

    async def _stream(
        self,
        messages: Sequence[Any],
        tools: list[dict] | None,
        tool_choice: Any,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[ChatResponse, None]:
        """Stream chunks through, keeping S11's guarantees where they still apply.

        Three things change once a call becomes a stream, and each is handled
        here rather than by the caller:

        * **The deadline spans consumption.** ``wait_for`` around an async
          generator only bounds setup, so the old wrapper forced
          ``stream=False``; ``asyncio.timeout`` wraps the ``async for`` instead.
        * **The budget settles on the last chunk.** Usage only exists there.
          Until then the estimate stays reserved, and an abandoned stream
          refunds it.
        * **A retry is honest only before the first chunk.** Replaying tokens
          the client already rendered would duplicate the answer, so a failure
          after the first yield is raised as-is.
        """
        switches = self.runtime.switches
        tier, selected, prepared = await self._plan(messages)
        estimate = self.runtime.budget.estimate(prepared) if switches.budget else 0
        max_retries = self._max_retries if switches.retry else 0
        timeout = self.request_timeout if switches.timeout else None
        last_error: BaseException | None = None
        skipped_for_circuit = False

        for index, name in enumerate(self._candidates(selected)):
            if switches.circuit and not self.runtime.circuits.allow(name):
                skipped_for_circuit = True
                log_guard_event("circuit.skip", model=name, reason="circuit_open")
                continue
            admission: Admission | None = None
            if switches.budget:
                admission = await self.runtime.budget.acquire_async(estimate)
                if not admission.granted:
                    raise self._exhausted("acquire_refused")
            if index > 0 or skipped_for_circuit:
                reason = "circuit_open" if skipped_for_circuit else "retry_exhausted"
                self._fallback_event(name, reason, tier)

            for attempt in range(max_retries + 1):
                yielded = False
                completed: ChatResponse | None = None
                settled = False
                retrying = False
                try:
                    with span(
                        "model.call",
                        model=name,
                        tier=tier.value,
                        attempt=attempt + 1,
                        fallback=index > 0 or skipped_for_circuit,
                    ) as call_span:
                        pending = self._model_factory(name)(
                            prepared,
                            tools=tools,
                            tool_choice=tool_choice,
                            **kwargs,
                        )
                        # A streaming delegate's ``__call__`` is a coroutine
                        # that resolves to the generator; a non-streaming one
                        # would resolve to a ChatResponse. Both are handled
                        # rather than assumed, because the wrapper's job is to
                        # preserve the delegate's shape.
                        resolved: Any = (
                            await pending if not inspect.isasyncgen(pending) else pending
                        )
                        if not inspect.isasyncgen(resolved):
                            raise TypeError(
                                "a streaming guarded model requires a streaming delegate"
                            )
                        # ``isasyncgen`` proves the shape; the element type is
                        # only knowable from the delegate's annotation.
                        stream = cast("AsyncGenerator[ChatResponse, None]", resolved)
                        async with asyncio.timeout(timeout):
                            async for chunk in stream:
                                yielded = True
                                if getattr(chunk, "is_last", False):
                                    completed = chunk
                                    # S13-3: what the provider reported.
                                    call_span.attributes.update(record_usage(chunk))
                                yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified below
                    if yielded or not _retryable(exc):
                        if switches.circuit:
                            self.runtime.circuits.record_failure(name)
                        raise
                    if attempt < max_retries:
                        self.runtime.metrics.incr("model.retry")
                        log_guard_event("model.retry", model=name, count=attempt + 1)
                        if self._backoff:
                            await asyncio.sleep(self._backoff * (2**attempt))
                        # Keep the reservation: nothing was billed, and the
                        # retry must settle against the same estimate.
                        retrying = True
                        continue
                    if switches.circuit:
                        self.runtime.circuits.record_failure(name)
                    last_error = exc
                    break
                else:
                    await self._settle(admission, completed)
                    settled = True
                    if switches.circuit:
                        self.runtime.circuits.record_success(name)
                    return
                finally:
                    # Nothing was billed yet: a cancelled consumer, an
                    # abandoned generator and a failed setup all land here.
                    # A retry is the exception — it keeps the reservation.
                    if admission is not None and not settled and not retrying:
                        await self.runtime.budget.release_async(admission.reserved)

        if last_error is not None:
            raise last_error
        raise self._exhausted("no_candidate")
