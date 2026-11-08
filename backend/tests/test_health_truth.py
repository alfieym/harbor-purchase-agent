"""S13-4: /health tells the truth when a dependency is down."""

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.features.health.router import router
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.service import OrderService
from harbor.features.orders.store import SqliteOrderStore


class _BrokenService:
    def ping(self) -> None:
        raise sqlite3.OperationalError("unable to open database file")


class _HealthyService:
    def ping(self) -> None:
        return None


def _app(order_service) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.settings = Settings(_env_file=None)
    app.state.order_service = order_service
    return app


def test_health_is_ok_when_the_database_answers() -> None:
    with TestClient(_app(_HealthyService())) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dependencies"]["database"] == "ok"


def test_health_is_degraded_and_503_when_the_database_is_down() -> None:
    with TestClient(_app(_BrokenService())) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"] == "error"


def test_order_ping_fails_when_the_schema_is_gone(tmp_path) -> None:
    service = OrderService(
        SqliteOrderStore(tmp_path / "orders.sqlite3"),
        SqliteEvidenceStore(tmp_path / "orders.sqlite3"),
    )

    service.ping()  # healthy at first

    (tmp_path / "orders.sqlite3").unlink()
    # A recreated empty file is not a working database: the schema is gone.
    try:
        service.ping()
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("ping() must fail when the schema disappeared")
