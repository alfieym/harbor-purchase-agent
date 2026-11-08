"""S13 observability HTTP surface and the request middleware."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.main import create_app
from harbor.observability import Observability, configure_observability
from harbor.observability.middleware import observability_middleware
from harbor.observability.router import router


def _app(obs: Observability | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.observability = obs
    app.state.settings = Settings(_env_file=None)
    return app


def _traced_app(obs: Observability | None) -> FastAPI:
    app = _app(obs)
    app.middleware("http")(observability_middleware)
    return app


def test_traces_list_detail_and_404() -> None:
    obs = Observability.create()
    with obs.tracer.trace_scope("turn", session_id="s1") as span_handle:
        with obs.tracer.span("agent.reply"):
            pass
        span_handle.attributes["outcome"] = "ok"

    with TestClient(_app(obs)) as client:
        listing = client.get("/api/observability/traces").json()
        assert listing["enabled"] is True
        summary = listing["traces"][0]
        assert summary["name"] == "turn"
        assert summary["session_id"] == "s1"
        assert summary["span_count"] == 2

        detail = client.get(f"/api/observability/traces/{summary['trace_id']}").json()
        assert detail["trace"]["trace_id"] == summary["trace_id"]
        assert [item["name"] for item in detail["trace"]["spans"]] == ["turn", "agent.reply"]

        assert client.get("/api/observability/traces/missing").status_code == 404


def test_metrics_and_status_endpoints() -> None:
    obs = Observability.create()
    obs.metrics.incr("turn.count")
    obs.metrics.observe("turn.latency_ms", 12.5)
    obs.events.publish("turn.started", session_id="s1")

    with TestClient(_app(obs)) as client:
        metrics = client.get("/api/observability/metrics").json()
        assert metrics["metrics"]["counters"]["turn.count"] == 1
        assert metrics["metrics"]["histograms"]["turn.latency_ms"]["count"] == 1

        status = client.get("/api/observability/status").json()
        assert status["enabled"] is True
        assert status["trace_count"] == 0
        assert status["event_count"] == 1


def test_events_recent_endpoint_filters_by_session() -> None:
    obs = Observability.create()
    obs.events.publish("turn.started", session_id="s1")
    obs.events.publish("turn.started", session_id="s2")

    with TestClient(_app(obs)) as client:
        body = client.get("/api/observability/events", params={"session_id": "s1"}).json()

    assert [event["type"] for event in body["events"]] == ["turn.started"]
    assert body["events"][0]["session_id"] == "s1"


def test_endpoints_report_disabled_without_an_instance() -> None:
    with TestClient(_app(None)) as client:
        assert client.get("/api/observability/status").json() == {"enabled": False}
        assert client.get("/api/observability/traces").json() == {
            "enabled": False,
            "traces": [],
        }
        assert client.get("/api/observability/events").json() == {
            "enabled": False,
            "events": [],
        }


def test_router_does_not_serve_another_apps_instance() -> None:
    # An app assembled without observability must report disabled, not read the
    # process-global instance that another app installed.
    other = Observability.create()
    other.events.publish("turn.started", session_id="s1")
    previous = configure_observability(other)
    try:
        with TestClient(_app(None)) as client:
            assert client.get("/api/observability/status").json() == {"enabled": False}
            assert client.get("/api/observability/events").json()["events"] == []
    finally:
        configure_observability(previous)


def test_create_app_reports_disabled_before_its_lifespan_runs() -> None:
    app = create_app(Settings(_env_file=None))
    assert app.state.observability is None


def test_repeated_client_request_id_keeps_traces_distinct() -> None:
    obs = Observability.create()
    with TestClient(_traced_app(obs)) as client:
        first = client.get("/health", headers={"x-request-id": "fixed-id"})
        second = client.get("/health", headers={"x-request-id": "fixed-id"})

    assert first.headers["x-trace-id"] != second.headers["x-trace-id"]
    assert len(obs.traces) == 2
    assert {trace.attributes["request_id"] for trace in obs.traces.recent()} == {"fixed-id"}


def test_unhandled_exception_is_counted_as_a_500() -> None:
    obs = Observability.create()
    app = _traced_app(obs)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500

    counters = obs.metrics.snapshot()["counters"]
    assert counters["http.requests"] == 1
    assert counters["http.status.500"] == 1
    trace = obs.traces.recent()[0]
    assert trace.status == "error"
    assert trace.spans[0].attributes["status_code"] == 500


def test_event_stream_resumes_from_last_event_id() -> None:
    obs = Observability.create()
    obs.events.publish("first", session_id="s1")
    obs.events.publish("second", session_id="s1")

    with TestClient(_app(obs)) as client:
        with client.stream(
            "GET",
            "/api/observability/events/stream",
            params={"session_id": "s1"},
            headers={"last-event-id": "1"},
        ) as response:
            first_line = next(line for line in response.iter_lines() if line)

    assert first_line == "id: 2"


def test_create_app_mounts_the_routes_and_the_request_middleware() -> None:
    app = create_app(Settings(_env_file=None))
    paths = app.openapi()["paths"]

    assert "/api/observability/status" in paths
    assert "/api/observability/traces" in paths
    assert "/api/observability/metrics" in paths
    assert "/api/observability/events" in paths
    assert "/api/observability/events/stream" in paths

    with TestClient(app) as client:
        response = client.get("/health")
        obs = app.state.observability

    assert response.headers["x-trace-id"]
    assert obs is not None
    trace = obs.traces.recent()[0]
    assert trace.name == "http.request"
    assert trace.attributes["path"] == "/health"
    assert trace.spans[0].attributes["status_code"] == 200
    assert obs.metrics.snapshot()["counters"]["http.requests"] == 1
