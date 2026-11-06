"""S10: OrderService application actions — thin translations over the store."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import (
    DraftRequestItem,
    OrderConflict,
    OrderNotFound,
    OrderService,
)
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


async def _draft(service: OrderService, *, qty: int = 1):
    return (
        await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", qty)],
            context_evidence=[_evidence()],
        )
    ).draft


async def _order(service: OrderService):
    draft = await _draft(service)
    order, _ = await service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
    return order


def test_other_buyer_cannot_see_or_confirm_draft(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        draft = await _draft(service)
        with pytest.raises(OrderNotFound):
            await service.confirm_draft(draft_id=draft.draft_id, buyer_id="b2")
        with pytest.raises(OrderNotFound):
            await service.get_order(order_id="ord_missing", buyer_id="b2")

    asyncio.run(scenario())


def test_conflict_reports_current_status(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        old = await _draft(service)
        await _draft(service, qty=2)
        return old

    old = asyncio.run(scenario())
    with pytest.raises(OrderConflict) as excinfo:
        asyncio.run(_service(tmp_path).confirm_draft(draft_id=old.draft_id, buyer_id="b1"))
    assert excinfo.value.current_status == "superseded"


def test_cancel_draft_is_idempotent(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        draft = await _draft(service)
        first, idem = await service.cancel_draft(draft_id=draft.draft_id, buyer_id="b1")
        again, idem2 = await service.cancel_draft(draft_id=draft.draft_id, buyer_id="b1")
        return first, idem, again, idem2

    first, idem, again, idem2 = asyncio.run(scenario())
    assert first.status == "cancelled"
    assert idem is False and idem2 is True
    assert again.status == "cancelled"


def test_cancellation_request_then_confirm_and_repeat(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        order = await _order(service)
        request, idem = await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        still, pending = await service.get_order(order_id=order.order_id, buyer_id="b1")
        cancelled, confirmed, idem2 = await service.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        again_order, again_request, idem3 = await service.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return (
            request,
            idem,
            still,
            pending,
            cancelled,
            confirmed,
            idem2,
            again_order,
            again_request,
            idem3,
        )

    (
        request,
        idem,
        still,
        pending,
        cancelled,
        confirmed,
        idem2,
        again_order,
        again_request,
        idem3,
    ) = asyncio.run(scenario())
    assert request.status == "pending" and idem is False
    assert still.status == "created"
    assert pending is not None and pending.request_id == request.request_id
    assert cancelled.status == "cancelled"
    assert confirmed.status == "confirmed"
    assert idem2 is False and idem3 is True
    assert again_order.order_id == cancelled.order_id
    assert again_request.request_id == confirmed.request_id


def test_withdraw_then_order_remains_created(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        order = await _order(service)
        await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        withdrawn, idem = await service.withdraw_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        still, request = await service.get_order(order_id=order.order_id, buyer_id="b1")
        return withdrawn, idem, still, request

    withdrawn, idem, still, request = asyncio.run(scenario())
    assert withdrawn.status == "withdrawn" and idem is False
    assert still.status == "created"
    assert request is not None and request.status == "withdrawn"


def test_withdraw_without_a_request_is_a_conflict(tmp_path) -> None:
    """Spec §5.3: "no request" is a state conflict (409), not "not found"."""

    async def scenario():
        service = _service(tmp_path)
        order = await _order(service)
        with pytest.raises(OrderConflict) as excinfo:
            await service.withdraw_cancellation(order_id=order.order_id, buyer_id="b1")
        with pytest.raises(OrderNotFound):
            await service.withdraw_cancellation(order_id="ord_missing", buyer_id="b1")
        return excinfo.value

    assert asyncio.run(scenario()).current_status is None


def test_request_on_cancelled_order_conflicts(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        order = await _order(service)
        await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        await service.confirm_cancellation(order_id=order.order_id, buyer_id="b1")
        with pytest.raises(OrderConflict) as excinfo:
            await service.request_cancellation(
                order_id=order.order_id, buyer_id="b1", idempotency_key="k2"
            )
        return excinfo.value

    assert asyncio.run(scenario()).current_status == "cancelled"


def test_get_order_returns_latest_cancellation(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        order = await _order(service)
        first, _ = await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        second, _ = await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k2"
        )
        _, latest = await service.get_order(order_id=order.order_id, buyer_id="b1")
        return first, second, latest

    first, second, latest = asyncio.run(scenario())
    assert latest is not None
    assert latest.request_id == second.request_id
    assert latest.request_id != first.request_id
