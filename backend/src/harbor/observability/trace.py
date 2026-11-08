"""Turn tracing for S13: one trace per request, one span per meaningful step.

A ``Span`` is a wall-clock interval with a parent, a status and free-form
attributes. Spans live in a ``Trace`` stored in a bounded in-memory ring; the
trace is the answer to "where did this turn's time go". The tracer is a
context manager, so instrumentation is one ``with span(...)`` around the code
being measured, including code that awaits.

Spans and traces also feed the metric registry: every finished span adds to
``span.<name>.latency_ms`` and every failed span bumps ``span.<name>.errors``.
That is how the trace line and the metrics line stay consistent without two
hand-maintained lists of timers.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar, Token
from typing import Any

from harbor.observability.metrics import MetricRegistry

__all__ = [
    "Span",
    "SpanContext",
    "Trace",
    "TraceStore",
    "Tracer",
    "current_trace_id",
]

_current_trace: ContextVar[Trace | None] = ContextVar("harbor_current_trace", default=None)
_current_span: ContextVar[Span | None] = ContextVar("harbor_current_span", default=None)

_MAX_ERROR_CHARS = 200


def _new_id() -> str:
    return uuid.uuid4().hex


def _error_text(exc: BaseException) -> str:
    detail = str(exc).strip().replace("\n", " ")
    if len(detail) > _MAX_ERROR_CHARS:
        detail = detail[: _MAX_ERROR_CHARS - 3] + "..."
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _duration_ms(started_monotonic: float) -> float:
    return round((time.perf_counter() - started_monotonic) * 1000, 3)


class Span:
    """One measured interval inside a trace."""

    __slots__ = (
        "span_id",
        "parent_id",
        "name",
        "started_at",
        "duration_ms",
        "status",
        "error",
        "attributes",
        "_started_monotonic",
    )

    def __init__(self, name: str, *, parent_id: str | None = None) -> None:
        self.span_id = _new_id()
        self.parent_id = parent_id
        self.name = name
        self.started_at = time.time()
        self.duration_ms: float | None = None
        self.status = "ok"
        self.error: str | None = None
        self.attributes: dict[str, Any] = {}
        self._started_monotonic = time.perf_counter()

    def finish(self, exc: BaseException | None = None) -> None:
        if self.duration_ms is not None:
            return
        self.duration_ms = _duration_ms(self._started_monotonic)
        if exc is not None:
            self.status = "error"
            self.error = _error_text(exc)

    def fail(self, reason: str) -> None:
        self.status = "error"
        self.error = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
        }


class Trace:
    """A root span plus every span recorded beneath it."""

    def __init__(self, *, name: str, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or _new_id()
        self.name = name
        self.started_at = time.time()
        self.duration_ms: float | None = None
        self.status = "ok"
        self.attributes: dict[str, Any] = {}
        self.spans: list[Span] = []
        self._started_monotonic = time.perf_counter()
        self._lock = threading.Lock()

    @property
    def root_span_id(self) -> str | None:
        return self.spans[0].span_id if self.spans else None

    @property
    def session_id(self) -> str | None:
        value = self.attributes.get("session_id")
        return value if isinstance(value, str) else None

    def add(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)

    def note_attributes(self, attributes: dict[str, Any]) -> None:
        """Merge attributes without overwriting what the trace already knows.

        A joined scope (a turn inside the HTTP middleware's trace) owns the
        identifiers the trace is filtered by, so they must land on the trace,
        not only on the span.
        """
        with self._lock:
            for key, value in attributes.items():
                self.attributes.setdefault(key, value)

    def finish(self) -> None:
        if self.duration_ms is not None:
            return
        self.duration_ms = _duration_ms(self._started_monotonic)
        with self._lock:
            if any(span.status == "error" for span in self.spans):
                self.status = "error"

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "span_count": len(self.spans),
            "session_id": self.session_id,
        }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            spans = [span.to_dict() for span in self.spans]
        return {
            **self.summary(),
            "attributes": dict(self.attributes),
            "spans": spans,
        }


class TraceStore:
    """Bounded newest-first store; the oldest trace is dropped at capacity."""

    def __init__(self, max_traces: int = 256) -> None:
        if max_traces < 1:
            raise ValueError("max_traces must be at least 1")
        self._max = max_traces
        self._order: deque[str] = deque()
        self._by_id: dict[str, Trace] = {}
        self._lock = threading.Lock()

    def add(self, trace: Trace) -> None:
        with self._lock:
            if trace.trace_id in self._by_id:
                return
            while len(self._order) >= self._max:
                oldest = self._order.popleft()
                self._by_id.pop(oldest, None)
            self._order.append(trace.trace_id)
            self._by_id[trace.trace_id] = trace

    def get(self, trace_id: str) -> Trace | None:
        with self._lock:
            return self._by_id.get(trace_id)

    def recent(self, *, limit: int = 50, session_id: str | None = None) -> list[Trace]:
        with self._lock:
            traces = [
                self._by_id[trace_id]
                for trace_id in reversed(self._order)
                if trace_id in self._by_id
            ]
        if session_id is not None:
            traces = [trace for trace in traces if trace.session_id == session_id]
        return traces[: max(0, limit)]

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            self._by_id.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)


class SpanContext:
    """Context manager returned by ``Tracer.span`` / ``Tracer.trace_scope``."""

    __slots__ = (
        "_tracer",
        "_span",
        "_trace",
        "_starts_trace",
        "_trace_token",
        "_span_token",
    )

    _trace_token: Token[Trace | None] | None
    _span_token: Token[Span | None] | None

    def __init__(
        self,
        tracer: Tracer,
        span: Span,
        *,
        trace: Trace,
        starts_trace: bool,
    ) -> None:
        self._tracer = tracer
        self._span = span
        self._trace = trace
        self._starts_trace = starts_trace
        self._trace_token = None
        self._span_token = None

    def __enter__(self) -> Span:
        if self._starts_trace:
            self._trace_token = _current_trace.set(self._trace)
        self._span_token = _current_span.set(self._span)
        self._trace.add(self._span)
        return self._span

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Returning None (rather than False) is what tells both mypy and a
        # reader that this context manager never swallows an exception; a
        # ``-> bool`` here would make mypy assume it might.
        try:
            self._span.finish(exc)
            self._tracer.record_span(self._span)
            if self._starts_trace:
                self._trace.finish()
                self._tracer.record_trace(self._trace)
        finally:
            if self._span_token is not None:
                _current_span.reset(self._span_token)
            if self._trace_token is not None:
                _current_trace.reset(self._trace_token)


class Tracer:
    """Creates spans, joins the ambient trace, and persists finished traces."""

    def __init__(self, store: TraceStore, metrics: MetricRegistry | None = None) -> None:
        self._store = store
        self._metrics = metrics

    def span(self, name: str, **attributes: Any) -> SpanContext:
        """Nest a span under the ambient trace, or start a new trace with it."""
        trace = _current_trace.get()
        if trace is None:
            return self.trace_scope(name, **attributes)
        parent = _current_span.get()
        span = Span(name, parent_id=parent.span_id if parent is not None else None)
        span.attributes.update(attributes)
        return SpanContext(self, span, trace=trace, starts_trace=False)

    def trace_scope(
        self, name: str, *, trace_id: str | None = None, **attributes: Any
    ) -> SpanContext:
        """Start a trace, or add a child span when one is already active."""
        trace = _current_trace.get()
        if trace is not None:
            if attributes:
                trace.note_attributes(attributes)
            return self.span(name, **attributes)
        trace = Trace(name=name, trace_id=trace_id)
        trace.attributes.update(attributes)
        root = Span(name)
        root.attributes.update(attributes)
        return SpanContext(self, root, trace=trace, starts_trace=True)

    def record_span(self, span: Span) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.observe(f"span.{span.name}.latency_ms", span.duration_ms or 0.0)
            if span.status == "error":
                self._metrics.incr(f"span.{span.name}.errors")
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass

    def record_trace(self, trace: Trace) -> None:
        try:
            self._store.add(trace)
            if self._metrics is None:
                return
            self._metrics.incr("trace.count")
            self._metrics.observe("trace.latency_ms", trace.duration_ms or 0.0)
            if trace.status == "error":
                self._metrics.incr("trace.errors")
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass


def current_trace_id() -> str | None:
    trace = _current_trace.get()
    return trace.trace_id if trace is not None else None
