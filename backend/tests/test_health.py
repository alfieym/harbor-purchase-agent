from datetime import datetime

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "harbor-purchase-agent"
    assert body["version"] == "0.1.0"
    assert body["environment"] == "test"
    assert body["llm_configured"] is False
    assert datetime.fromisoformat(body["time"]).tzinfo is not None


def test_unknown_path_returns_error_envelope(client: TestClient) -> None:
    response = client.get("/api/unknown")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]
