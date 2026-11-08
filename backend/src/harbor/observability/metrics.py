"""Process-wide metric registry for S13.

Counters answer "how often", gauges answer "what is the current value", and
rolling-window histograms answer "is it getting worse" without keeping every
observation forever. The window is deliberately bounded: a process that runs
for days should report the recent p95, not the p95 of last week.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Sequence
from typing import Any

__all__ = ["MetricRegistry", "summarize"]


def _percentile(ordered: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile over an already sorted sequence."""
    rank = max(1, math.ceil(percent / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarize(values: Sequence[float]) -> dict[str, float | int]:
    """Count, range and nearest-rank percentiles for one histogram window."""
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    return {
        "count": count,
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "mean": round(sum(ordered) / count, 3),
        "p50": round(_percentile(ordered, 50), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "p99": round(_percentile(ordered, 99), 3),
    }


class MetricRegistry:
    """Thread-safe counters, gauges and bounded histograms.

    Spans feed this registry automatically, so a component that is instrumented
    with a span (`span.<name>.latency_ms`) also gets a latency distribution for
    free. Business counters (`tool.search.empty`, `http.status.502`) are added
    explicitly at the call site.
    """

    def __init__(self, window: int = 512) -> None:
        if window < 1:
            raise ValueError("window must be at least 1")
        self._window = window
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def incr(self, name: str, count: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + count

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            window = self._histograms.get(name)
            if window is None:
                window = deque(maxlen=self._window)
                self._histograms[name] = window
            window.append(float(value))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            histograms = {
                name: summarize(list(window)) for name, window in self._histograms.items()
            }
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": histograms,
            }

    def name_count(self) -> int:
        with self._lock:
            return len(self._counters) + len(self._gauges) + len(self._histograms)

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
