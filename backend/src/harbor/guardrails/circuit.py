"""Per-model circuit breaker (spec §5.3).

Only retry-exhausted failures are recorded; a 4xx is the caller's fault and
must not put the provider into cooldown. State lives in this process, so a
restart clears it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from harbor.guardrails.events import GuardMetrics, log_guard_event


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Entry:
    failures: int = 0
    opened_at: float | None = None
    half_open: bool = False


class CircuitBreaker:
    def __init__(
        self,
        threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        *,
        metrics: GuardMetrics | None = None,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._metrics = metrics
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _entry(self, model: str) -> _Entry:
        return self._entries.setdefault(model, _Entry())

    def allow(self, model: str) -> bool:
        with self._lock:
            entry = self._entry(model)
            if entry.opened_at is None:
                return True
            if entry.half_open:
                # One probe at a time; everyone else still sees an open circuit.
                return False
            if self._clock() - entry.opened_at >= self._cooldown:
                entry.half_open = True
                return True
            return False

    def record_success(self, model: str) -> None:
        with self._lock:
            entry = self._entry(model)
            was_open = entry.opened_at is not None
            entry.failures = 0
            entry.opened_at = None
            entry.half_open = False
        if was_open:
            self._count("circuit.close")
            log_guard_event("circuit.close", model=model)

    def record_failure(self, model: str) -> None:
        with self._lock:
            entry = self._entry(model)
            if entry.half_open:
                entry.half_open = False
                entry.opened_at = self._clock()
                opened = True
            else:
                entry.failures += 1
                opened = entry.failures >= self._threshold
                if opened:
                    entry.opened_at = self._clock()
        if opened:
            self._count("circuit.open")
            log_guard_event("circuit.open", model=model)

    def state(self, model: str) -> CircuitState:
        with self._lock:
            entry = self._entries.get(model)
            if entry is None or entry.opened_at is None:
                return CircuitState.CLOSED
            return CircuitState.HALF_OPEN if entry.half_open else CircuitState.OPEN

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                model: (
                    CircuitState.HALF_OPEN.value
                    if entry.half_open
                    else (CircuitState.OPEN.value if entry.opened_at is not None else CircuitState.CLOSED.value)
                )
                for model, entry in self._entries.items()
            }

    def _count(self, name: str) -> None:
        if self._metrics is not None:
            self._metrics.incr(name)
