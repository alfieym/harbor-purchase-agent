"""The three S13 observability lines, assembled once per process."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from harbor.observability.events import EventBus
from harbor.observability.metrics import MetricRegistry
from harbor.observability.trace import Tracer, TraceStore

__all__ = ["Observability"]


@dataclass
class Observability:
    """Trace store, metric registry and event bus for one application."""

    metrics: MetricRegistry
    events: EventBus
    traces: TraceStore
    tracer: Tracer
    started_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        *,
        trace_buffer: int = 256,
        event_buffer: int = 200,
        metric_window: int = 512,
        global_event_buffer: int = 1000,
    ) -> Observability:
        metrics = MetricRegistry(window=metric_window)
        traces = TraceStore(max_traces=trace_buffer)
        events = EventBus(session_buffer=event_buffer, global_buffer=global_event_buffer)
        return cls(
            metrics=metrics,
            events=events,
            traces=traces,
            tracer=Tracer(traces, metrics),
        )
