"""Structured guardrail events + in-memory counters (spec §9).

The whitelist is the whole point: a log line must never carry message text,
tool output or model output, so only these fields are ever emitted.
"""

from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

EVENT_FIELDS = frozenset(
    {"event", "tier", "model", "reason", "rule", "tool", "count", "session_id"}
)


class GuardMetrics:
    """Process-wide counters, exposed read-only through the status endpoint."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def incr(self, name: str, *, count: int = 1) -> None:
        self._counts[name] += count

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


def log_guard_event(name: str, **fields: str | int | None) -> None:
    """Emit one whitelisted guard event.

    Non-whitelisted keys are kept as ``None`` rather than dropped so the log
    schema stays stable, and their values are discarded — never logged.
    """
    payload: dict[str, str | int | None] = {key: None for key in EVENT_FIELDS}
    payload.update({key: value for key, value in fields.items() if key in EVENT_FIELDS})
    payload.update({key: None for key in fields if key not in EVENT_FIELDS})
    payload["event"] = name
    logger.info("guard.event", extra={"guard": payload})
