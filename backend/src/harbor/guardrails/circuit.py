"""Per-key circuit breaker (spec §5.3).

One state machine serves two callers: models (keyed by model name) and tools
(keyed by tool name, in a separate instance with ``kind="tool"``). Only
retry-exhausted failures are recorded; a 4xx is the caller's fault and must
not put the provider into cooldown. State lives in this process, so a restart
clears it.
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
        kind: str = "model",
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._metrics = metrics
        # Which log field carries the key: ``model`` or ``tool`` (whitelist).
        self._kind = kind
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _entry(self, key: str) -> _Entry:
        return self._entries.setdefault(key, _Entry())

    def allow(self, key: str) -> bool:
        with self._lock:
            entry = self._entry(key)
            if entry.opened_at is None:
                return True
            if entry.half_open:
                # One probe at a time; everyone else still sees an open circuit.
                return False
            if self._clock() - entry.opened_at >= self._cooldown:
                entry.half_open = True
                return True
            return False

    def record_success(self, key: str) -> None:
        with self._lock:
            entry = self._entry(key)
            was_open = entry.opened_at is not None
            entry.failures = 0
            entry.opened_at = None
            entry.half_open = False
        if was_open:
            self._count("circuit.close")
            log_guard_event("circuit.close", **{self._kind: key})

    def record_failure(self, key: str) -> None:
        with self._lock:
            entry = self._entry(key)
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
            log_guard_event("circuit.open", **{self._kind: key})

    def state(self, key: str) -> CircuitState:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.opened_at is None:
                return CircuitState.CLOSED
            return CircuitState.HALF_OPEN if entry.half_open else CircuitState.OPEN

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                key: (
                    CircuitState.HALF_OPEN.value
                    if entry.half_open
                    else (CircuitState.OPEN.value if entry.opened_at is not None else CircuitState.CLOSED.value)
                )
                for key, entry in self._entries.items()
            }

    def _count(self, name: str) -> None:
        if self._metrics is not None:
            self._metrics.incr(name)
