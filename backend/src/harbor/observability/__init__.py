"""S13 observability: trace, metrics and a user-visible event bus.

Call sites import the small helpers from this package (``span``,
``trace_scope``, ``publish_event``, ``incr``) instead of reaching into a
runtime object. When no ``Observability`` is configured — the CLI, the eval
runner, unit tests — every helper is a no-op, so instrumentation never changes
behaviour.
"""

from __future__ import annotations

import threading
from typing import Any

from harbor.observability.core import Observability
from harbor.observability.events import EventBus, Subscription, UserEvent
from harbor.observability.metrics import MetricRegistry, summarize
from harbor.observability.trace import (
    Span,
    SpanContext,
    Trace,
    Tracer,
    TraceStore,
    current_trace_id,
)

__all__ = [
    "EventBus",
    "MetricRegistry",
    "NullSpan",
    "Observability",
    "Span",
    "SpanContext",
    "Subscription",
    "Trace",
    "TraceStore",
    "Tracer",
    "UserEvent",
    "configure_observability",
    "current_trace_id",
    "get_observability",
    "incr",
    "observe",
    "publish_event",
    "span",
    "summarize",
    "trace_scope",
]

_current: Observability | None = None
_lock = threading.Lock()


def configure_observability(instance: Observability | None) -> Observability | None:
    """Install the process-wide instance; returns the previous one."""
    global _current
    with _lock:
        previous = _current
        _current = instance
    return previous


def get_observability() -> Observability | None:
    return _current


class NullSpan:
    """Stand-in context manager when observability is not configured."""

    __slots__ = ("attributes",)

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(self, *exc: Any) -> None:
        # ``None`` rather than ``False``: the no-op span never swallows an
        # exception, and a bool return would tell mypy it might.
        return None

    def fail(self, reason: str) -> None:
        pass


def span(name: str, **attributes: Any) -> SpanContext | NullSpan:
    instance = get_observability()
    if instance is None:
        return NullSpan()
    return instance.tracer.span(name, **attributes)


def trace_scope(
    name: str = "turn", *, trace_id: str | None = None, **attributes: Any
) -> SpanContext | NullSpan:
    instance = get_observability()
    if instance is None:
        return NullSpan()
    return instance.tracer.trace_scope(name, trace_id=trace_id, **attributes)


def publish_event(
    event_type: str, *, session_id: str | None = None, **data: Any
) -> UserEvent | None:
    instance = get_observability()
    if instance is None:
        return None
    return instance.events.publish(event_type, session_id=session_id, **data)


def incr(name: str, count: int = 1) -> None:
    instance = get_observability()
    if instance is not None:
        instance.metrics.incr(name, count)


def observe(name: str, value: float) -> None:
    instance = get_observability()
    if instance is not None:
        instance.metrics.observe(name, value)
