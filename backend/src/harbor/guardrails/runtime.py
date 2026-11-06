"""Process-wide guard state (spec §4.3).

Built once per application and shared by the main agent, the S9 sub-agents
and S7's summary calls, because the model object itself is shared.
"""

from __future__ import annotations

from dataclasses import dataclass

from harbor.core.config import Settings
from harbor.guardrails.budget import BudgetTier, SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.session import GuardSession

RULE_FALLBACK_TEXT = "今天的回答额度已用完，请稍后再来，或联系人工客服处理。"


@dataclass
class GuardRuntime:
    budget: SqliteTokenBudget
    circuits: CircuitBreaker
    metrics: GuardMetrics
    lite_model: str | None = None
    fallback_model: str | None = None
    last_tier: BudgetTier | None = None
    loop_window: int = 8
    loop_threshold: int = 3
    tool_output_max_chars: int = 8000

    @classmethod
    def from_settings(cls, settings: Settings) -> GuardRuntime:
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
            ),
            metrics=GuardMetrics(),
            lite_model=settings.guard_lite_model,
            fallback_model=settings.guard_fallback_model,
            loop_window=settings.guard_loop_window,
            loop_threshold=settings.guard_loop_threshold,
            tool_output_max_chars=settings.guard_tool_output_max_chars,
        )

    def new_session(self) -> GuardSession:
        """Fresh per-agent loop/sequence state; never shared between agents."""
        return GuardSession(
            loop_window=self.loop_window, loop_threshold=self.loop_threshold
        )

    def rule_fallback_text(self) -> str:
        """Fixed copy for the turn-level short circuit (spec §5.5)."""
        return RULE_FALLBACK_TEXT
