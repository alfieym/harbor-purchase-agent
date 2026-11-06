"""S10: application-layer order endpoints under /commerce/orders."""

import asyncio
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.router import router
from harbor.features.orders.service import DraftRequestItem, OrderService
from harbor.features.orders.store import SqliteOrderStore


def _service(tmp_path: Path) -> OrderService:
    path = tmp_path / "orders.sqlite3"
    return OrderService(SqliteOrderStore(path), SqliteEvidenceStore(path))


def _evidence() -> SearchEvidence:
    return SearchEvidence(
        product_id="P1",
        sku_id="P1-S1",
        title="猫饮水器",
        unit_price=Decimal("89.00"),
        currency="CNY",
    )


def _client(tmp_path) -> tuple[TestClient, OrderService]:
    service = _service(tmp_path)
    app = FastAPI()
    app.state.order_service = service
    app.include_router(router, prefix="/commerce")
    return TestClient(app), service


def _draft(service: OrderService, *, qty: int = 1):
    return asyncio.run(
        service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", qty)],
            context_evidence=[_evidence()],
        )
    ).draft


def _order(service: OrderService):
    draft = _draft(service)
    order, _ = asyncio.run(service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"))
    return order


def test_confirm_route_returns_order_and_conflicts_on_superseded(tmp_path) -> None:
    client, service = _client(tmp_path)
    old = _draft(service)
    _draft(service, qty=2)

    response = client.post(
        f"/commerce/orders/drafts/{old.draft_id}/confirm", json={"buyer_id": "b1"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["current_status"] == "superseded"


def test_pending_draft_route_survives_a_page_reload(tmp_path) -> None:
    client, service = _client(tmp_path)
    draft = _draft(service)

    response = client.get(
        "/commerce/orders/drafts",
        params={"buyer_id": "b1", "shopping_session_id": "s1"},
    )

    assert response.status_code == 200
    assert response.json()["draft_id"] == draft.draft_id
    assert response.json()["total"] == "89.00"


def test_missing_service_returns_503() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/commerce")
    response = TestClient(app).get(
        "/commerce/orders/drafts", params={"buyer_id": "b1", "shopping_session_id": "s1"}
    )
    assert response.status_code == 503


def test_confirm_route_is_idempotent(tmp_path) -> None:
    client, service = _client(tmp_path)
    draft = _draft(service)

    first = client.post(
        f"/commerce/orders/drafts/{draft.draft_id}/confirm", json={"buyer_id": "b1"}
    )
    again = client.post(
        f"/commerce/orders/drafts/{draft.draft_id}/confirm", json={"buyer_id": "b1"}
    )

    assert first.status_code == 200
    assert first.json()["idempotent"] is False
    assert first.json()["order"]["total"] == "89.00"
    assert again.json()["idempotent"] is True
    assert again.json()["order"]["order_id"] == first.json()["order"]["order_id"]


def test_non_owner_gets_404(tmp_path) -> None:
    client, service = _client(tmp_path)
    draft = _draft(service)
    order = _order(service)

    confirm = client.post(
        f"/commerce/orders/drafts/{draft.draft_id}/confirm", json={"buyer_id": "b2"}
    )
    read = client.get(f"/commerce/orders/{order.order_id}", params={"buyer_id": "b2"})

    assert confirm.status_code == 404
    assert read.status_code == 404


def test_cancel_draft_route(tmp_path) -> None:
    client, service = _client(tmp_path)
    draft = _draft(service)

    response = client.post(
        f"/commerce/orders/drafts/{draft.draft_id}/cancel", json={"buyer_id": "b1"}
    )

    assert response.status_code == 200
    assert response.json()["draft"]["status"] == "cancelled"
    pending = client.get(
        "/commerce/orders/drafts", params={"buyer_id": "b1", "shopping_session_id": "s1"}
    )
    # Spec §6.2: no pending draft is ``null`` (200), not a 404 — the page-reload
    # path must not need to special-case a missing draft as an error.
    assert pending.status_code == 200
    assert pending.json() is None


def test_cancellation_flow_routes(tmp_path) -> None:
    client, service = _client(tmp_path)
    order = _order(service)

    requested = client.post(
        f"/commerce/orders/{order.order_id}/cancellation",
        json={"buyer_id": "b1", "idempotency_key": "k1", "reason": "尺寸不合适"},
    )
    detail = client.get(f"/commerce/orders/{order.order_id}", params={"buyer_id": "b1"})
    confirmed = client.post(
        f"/commerce/orders/{order.order_id}/cancellation/confirm", json={"buyer_id": "b1"}
    )
    again = client.post(
        f"/commerce/orders/{order.order_id}/cancellation/confirm", json={"buyer_id": "b1"}
    )

    assert requested.status_code == 200
    assert requested.json()["cancellation"]["status"] == "pending"
    assert requested.json()["idempotent"] is False
    assert detail.json()["order"]["status"] == "created"
    assert (
        detail.json()["cancellation"]["request_id"]
        == (requested.json()["cancellation"]["request_id"])
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["order"]["status"] == "cancelled"
    assert confirmed.json()["idempotent"] is False
    assert again.json()["idempotent"] is True


def test_withdraw_route_keeps_order_created(tmp_path) -> None:
    client, service = _client(tmp_path)
    order = _order(service)
    requested = client.post(
        f"/commerce/orders/{order.order_id}/cancellation",
        json={"buyer_id": "b1", "idempotency_key": "k1"},
    )

    withdrawn = client.post(
        f"/commerce/orders/{order.order_id}/cancellation/withdraw", json={"buyer_id": "b1"}
    )
    detail = client.get(f"/commerce/orders/{order.order_id}", params={"buyer_id": "b1"})

    assert requested.status_code == 200
    assert withdrawn.status_code == 200
    assert withdrawn.json()["cancellation"]["status"] == "withdrawn"
    assert detail.json()["order"]["status"] == "created"


def test_get_missing_order_returns_404(tmp_path) -> None:
    client, _ = _client(tmp_path)
    response = client.get("/commerce/orders/ord_missing", params={"buyer_id": "b1"})
    assert response.status_code == 404
