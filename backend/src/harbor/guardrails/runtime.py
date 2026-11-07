"""Process-wide guard state (spec §4.3).

Built once per application and shared by the main agent, the S9 sub-agents
and S7's summary calls, because the model object itself is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harbor.core.config import Settings
from harbor.guardrails.budget import BudgetTier, SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.session import GuardSession

RULE_FALLBACK_TEXT = "今天的回答额度已用完，请稍后再来，或联系人工客服处理。"


@dataclass(frozen=True)
class GuardSwitches:
    """Per-guardrail switches (S11-8).

    ``guardrails_enabled`` is the kill switch: off removes the wrappers
    entirely. Each flag here turns off one behaviour *inside* the wrappers and
    restores that behaviour's pre-S11 baseline, without touching the other
    nine. Every layer reads them through ``GuardRuntime.switches``.
    """

    timeout: bool = True
    retry: bool = True
    circuit: bool = True
    fallback: bool = True
    budget: bool = True
    loop: bool = True
    sequence: bool = True
    structure: bool = True
    content_filter: bool = True
    output_guard: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "timeout": self.timeout,
            "retry": self.retry,
            "circuit": self.circuit,
            "fallback": self.fallback,
            "budget": self.budget,
            "loop": self.loop,
            "sequence": self.sequence,
            "structure": self.structure,
            "content_filter": self.content_filter,
            "output_guard": self.output_guard,
        }


@dataclass
class GuardRuntime:
    budget: SqliteTokenBudget
    circuits: CircuitBreaker
    metrics: GuardMetrics
    # Tool failures and model failures are different signals; separate
    # instances keep one tool's outage from demoting the model (S11-1).
    tool_circuits: CircuitBreaker = field(
        default_factory=lambda: CircuitBreaker(3, 60.0, kind="tool")
    )
    lite_model: str | None = None
    fallback_model: str | None = None
    last_tier: BudgetTier | None = None
    loop_window: int = 8
    loop_threshold: int = 3
    tool_output_max_chars: int = 8000
    reply_max_chars: int = 4000
    switches: GuardSwitches = field(default_factory=GuardSwitches)

    @classmethod
    def from_settings(cls, settings: Settings) -> GuardRuntime:
        # One metrics instance shared with the breaker, so circuit.open /
        # circuit.close reach the status endpoint and the event stream.
        metrics = GuardMetrics()
        return cls(
            budget=SqliteTokenBudget(
                settings.guard_budget_db_path,
                settings.guard_daily_token_budget,
                chars_per_token=settings.guard_chars_per_token,
                safety_factor=settings.guard_estimate_safety_factor,
            ),
            circuits=CircuitBreaker(
                settings.guard_circuit_failure_threshold,
                settings.guard_circuit_cooldown_seconds,
                metrics=metrics,
            ),
            metrics=metrics,
            tool_circuits=CircuitBreaker(
                settings.guard_circuit_failure_threshold,
                settings.guard_circuit_cooldown_seconds,
                metrics=metrics,
                kind="tool",
            ),
            lite_model=settings.guard_lite_model,
            fallback_model=settings.guard_fallback_model,
            loop_window=settings.guard_loop_window,
            loop_threshold=settings.guard_loop_threshold,
            tool_output_max_chars=settings.guard_tool_output_max_chars,
            reply_max_chars=settings.guard_reply_max_chars,
            switches=GuardSwitches(
                timeout=settings.guard_timeout_enabled,
                retry=settings.guard_retry_enabled,
                circuit=settings.guard_circuit_enabled,
                fallback=settings.guard_fallback_enabled,
                budget=settings.guard_budget_enabled,
                loop=settings.guard_loop_enabled,
                sequence=settings.guard_sequence_enabled,
                structure=settings.guard_structure_enabled,
                content_filter=settings.guard_content_filter_enabled,
                output_guard=settings.guard_output_guard_enabled,
            ),
        )

    def new_session(self) -> GuardSession:
        """Fresh per-agent loop/sequence state; never shared between agents."""
        return GuardSession(
            loop_window=self.loop_window, loop_threshold=self.loop_threshold
        )

    def rule_fallback_text(self) -> str:
        """Fixed copy for the turn-level short circuit (spec §5.5)."""
        return RULE_FALLBACK_TEXT
