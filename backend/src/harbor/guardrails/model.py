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
from collections.abc import Sequence
from typing import Any

from agentscope.message import SystemMsg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from harbor.guardrails.budget import Admission, BudgetTier
from harbor.guardrails.events import log_guard_event
from harbor.guardrails.runtime import GuardRuntime

MINIMAL_MODE_HINT = (
    "当前处于预算紧张模式：最多再做一次必要检索，不要重复调用工具，"
    "请基于已有结果直接给出简洁回复。"
)


class BudgetExhausted(RuntimeError):
    """The daily token budget refused this call; no request was sent."""


def clone_model(delegate: ChatModelBase, name: str) -> ChatModelBase:
    """Same provider, key and base URL; only the model name changes."""
    clone = copy.copy(delegate)
    clone.model = name
    clone.max_retries = 0
    clone.stream = False
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
            # A timeout around an async generator only bounds request setup,
            # so streaming has to go for the budgets above to be meaningful.
            stream=False,
            max_retries=0,
            retry_delay=0.0,
            context_size=delegate.context_size,
        )
        self._delegate = delegate
        self._delegate.max_retries = 0
        self._delegate.stream = False
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
        if fallback and fallback != selected:
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

    def _settle(self, admission: Admission, response: ChatResponse) -> None:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if usage is None:
            self.runtime.budget.settle(admission.reserved, admission.reserved)
            return
        actual = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        self.runtime.budget.settle(admission.reserved, actual)

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

    async def __call__(
        self,
        messages: Sequence[Any],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        tier = self.runtime.budget.tier()
        if tier is BudgetTier.FALLBACK:
            # The turn-level gate should have caught this already; if it did
            # not, still refuse rather than send a doomed request.
            raise self._exhausted("tier_fallback")

        selected = self.model
        if (
            tier in (BudgetTier.LITE, BudgetTier.MINIMAL)
            and self.runtime.lite_model
        ):
            selected = self.runtime.lite_model
        if tier is not BudgetTier.MAIN:
            reason = (
                "budget_tier_lite" if tier is BudgetTier.LITE else "budget_tier_minimal"
            )
            self._fallback_event(selected, reason, tier)
            self.runtime.last_tier = tier

        prepared = self._prepare(messages, tier)
        estimate = self.runtime.budget.estimate(prepared)
        last_error: BaseException | None = None
        skipped_for_circuit = False

        for index, name in enumerate(self._candidates(selected)):
            if not self.runtime.circuits.allow(name):
                skipped_for_circuit = True
                log_guard_event("circuit.skip", model=name, reason="circuit_open")
                continue
            admission = self.runtime.budget.acquire(estimate)
            if not admission.granted:
                raise self._exhausted("acquire_refused")
            if index > 0 or skipped_for_circuit:
                reason = "circuit_open" if skipped_for_circuit else "retry_exhausted"
                self._fallback_event(name, reason, tier)

            for attempt in range(self._max_retries + 1):
                try:
                    response = await asyncio.wait_for(
                        self._model_factory(name)(
                            prepared,
                            tools=tools,
                            tool_choice=tool_choice,
                            **kwargs,
                        ),
                        timeout=self.request_timeout,
                    )
                except asyncio.CancelledError:
                    self.runtime.budget.release(admission.reserved)
                    raise
                except Exception as exc:  # noqa: BLE001 - classified below
                    if not _retryable(exc):
                        self.runtime.budget.release(admission.reserved)
                        raise
                    if attempt < self._max_retries:
                        self.runtime.metrics.incr("model.retry")
                        log_guard_event("model.retry", model=name, count=attempt + 1)
                        if self._backoff:
                            await asyncio.sleep(self._backoff * (2**attempt))
                        continue
                    self.runtime.budget.release(admission.reserved)
                    self.runtime.circuits.record_failure(name)
                    last_error = exc
                    break
                else:
                    self._settle(admission, response)
                    self.runtime.circuits.record_success(name)
                    return response

        if last_error is not None:
            raise last_error
        raise self._exhausted("no_candidate")
