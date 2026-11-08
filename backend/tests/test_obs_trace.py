"""S13 trace core: spans, nesting, error status, storage and metric bridges."""

import pytest

from harbor.observability import (
    Observability,
    configure_observability,
    get_observability,
    span,
    trace_scope,
)
from harbor.observability.trace import Tracer, TraceStore, current_trace_id


@pytest.fixture()
def obs():
    instance = Observability.create()
    previous = configure_observability(instance)
    yield instance
    configure_observability(previous)


def test_span_nests_under_the_turn_and_records_session(obs) -> None:
    with trace_scope("turn", session_id="s1", buyer_id="b1") as root:
        assert current_trace_id() is not None
        with span("model.call", model="m") as child:
            assert child.parent_id == root.span_id

    trace = obs.traces.recent()[0]
    assert trace.name == "turn"
    assert trace.attributes["session_id"] == "s1"
    assert [item.name for item in trace.spans] == ["turn", "model.call"]
    assert trace.status == "ok"
    assert trace.spans[0].duration_ms is not None


def test_error_sets_status_and_error_text(obs) -> None:
    with pytest.raises(ValueError):
        with trace_scope("turn"):
            with span("tool.product_search"):
                raise ValueError("boom")

    trace = obs.traces.recent()[0]
    assert trace.status == "error"
    failed = trace.spans[-1]
    assert failed.status == "error"
    assert "ValueError" in (failed.error or "")


def test_span_without_a_trace_starts_one(obs) -> None:
    with span("tool.product_search", hits=3):
        pass

    trace = obs.traces.recent()[0]
    assert trace.name == "tool.product_search"
    assert trace.spans[0].attributes["hits"] == 3


def test_nested_trace_scope_joins_the_existing_trace(obs) -> None:
    with trace_scope("turn") as root:
        with trace_scope("inner") as nested:
            assert nested.parent_id == root.span_id

    traces = obs.traces.recent()
    assert len(traces) == 1
    assert [item.name for item in traces[0].spans] == ["turn", "inner"]


def test_joining_trace_scope_records_session_on_the_trace(obs) -> None:
    # The HTTP middleware owns the trace; the turn joins it. The session must
    # land on the trace, or /traces?session_id= can never find the turn.
    with trace_scope("http.request", path="/commerce/intents"):
        with trace_scope("turn", session_id="s1", buyer_id="b1"):
            pass

    trace = obs.traces.recent()[0]
    assert trace.attributes["session_id"] == "s1"
    assert trace.attributes["buyer_id"] == "b1"
    assert trace.session_id == "s1"
    assert [item.trace_id for item in obs.traces.recent(session_id="s1")] == [trace.trace_id]


def test_store_keeps_the_newest_traces(obs) -> None:
    for index in range(3):
        with trace_scope("turn", index=index):
            pass

    assert len(obs.traces) == 3
    assert [trace.attributes["index"] for trace in obs.traces.recent()] == [2, 1, 0]

    small = TraceStore(max_traces=2)
    tracer = Tracer(small)
    for index in range(3):
        with tracer.trace_scope("turn", index=index):
            pass

    assert len(small) == 2
    assert [trace.attributes["index"] for trace in small.recent()] == [2, 1]


def test_spans_feed_latency_histograms_and_counters(obs) -> None:
    with trace_scope("turn"):
        with span("agent.reply"):
            pass

    snapshot = obs.metrics.snapshot()
    assert snapshot["histograms"]["span.turn.latency_ms"]["count"] == 1
    assert snapshot["histograms"]["span.agent.reply.latency_ms"]["count"] == 1
    assert snapshot["counters"]["trace.count"] == 1
    assert "trace.errors" not in snapshot["counters"]


def test_everything_is_a_noop_without_observability() -> None:
    previous = configure_observability(None)
    try:
        with trace_scope("turn") as span_handle:
            span_handle.attributes["ignored"] = True
        with span("model.call"):
            pass
        assert get_observability() is None
        assert current_trace_id() is None
    finally:
        configure_observability(previous)
