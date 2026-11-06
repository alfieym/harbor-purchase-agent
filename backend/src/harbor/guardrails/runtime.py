"""Process-wide guard state (spec §4.3).

Built once per application and shared by the main agent, the S9 sub-agents
and S7's summary calls, because the model object itself is shared.
"""

from __future__ import annotations

from dataclasses import dataclass

from harbor.guardrails.budget import BudgetTier, SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics


@dataclass
class GuardRuntime:
    budget: SqliteTokenBudget
    circuits: CircuitBreaker
    metrics: GuardMetrics
    lite_model: str | None = None
    fallback_model: str | None = None
    last_tier: BudgetTier | None = None
