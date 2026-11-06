from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.features.guardrails.router import router as guardrails_router
from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.runtime import GuardRuntime
from harbor.main import create_app


def _guard(tmp_path) -> GuardRuntime:
    return GuardRuntime(
        budget=SqliteTokenBudget(tmp_path / "budget.sqlite3", 1000),
        circuits=CircuitBreaker(3, 60.0),
        metrics=GuardMetrics(),
    )


def _client(guard: GuardRuntime | None) -> TestClient:
    app = FastAPI()
    app.include_router(guardrails_router, prefix="/api")
    app.state.runtime = SimpleNamespace(guard=guard) if guard is not None else None
    return TestClient(app)


def test_status_reports_budget_and_metrics(tmp_path) -> None:
    guard = _guard(tmp_path)
    guard.metrics.incr("model.retry")
    guard.budget.acquire(10)

    with _client(guard) as client:
        resp = client.get("/api/guardrails/status")

    body = resp.json()
    assert resp.status_code == 200
    assert body["enabled"] is True
    assert body["budget"]["cap"] == 1000
    assert body["budget"]["used"] == 10
    assert body["budget"]["tier"] == "main"
    assert body["metrics"]["model.retry"] == 1
    assert isinstance(body["circuits"], dict)


def test_status_is_disabled_without_a_runtime() -> None:
    with _client(None) as client:
        body = client.get("/api/guardrails/status").json()

    assert body == {"enabled": False, "budget": {}, "circuits": {}, "metrics": {}}


def test_create_app_mounts_the_status_route() -> None:
    app = create_app(Settings(_env_file=None))

    assert "/api/guardrails/status" in app.openapi()["paths"]
