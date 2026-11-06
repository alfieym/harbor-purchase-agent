"""S10: two-phase order cancellation — request, then a human decision."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.orders.models import NewDraftItem
from harbor.features.orders.store import (
    CancellationNotPending,
    OrderAlreadyCancelled,
    SqliteOrderStore,
)


def _item() -> NewDraftItem:
    return NewDraftItem(
        product_id="P1",
        sku_id="P1-S1",
        title="猫饮水器",
        quantity=1,
        unit_price=Decimal("89.00"),
        currency="CNY",
        evidence_unit_price=Decimal("89.00"),
        evidence_currency="CNY",
    )


def _store(tmp_path: Path) -> SqliteOrderStore:
    return SqliteOrderStore(tmp_path / "orders.sqlite3")


async def _order(store: SqliteOrderStore, buyer_id: str = "b1"):
    draft, _ = await store.create_draft(
        buyer_id=buyer_id,
        shopping_session_id="s1",
        currency="CNY",
        items=[_item()],
    )
    order, _ = await store.confirm_draft(draft_id=draft.draft_id, buyer_id=buyer_id)
    return order


def test_request_keeps_order_created_then_confirm_cancels_it(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        request, idem = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        still = await store.get_order(order_id=order.order_id, buyer_id="b1")
        confirmed_order, confirmed_req, idem2 = await store.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return order, request, idem, still, confirmed_order, confirmed_req, idem2

    order, request, idem, still, confirmed_order, confirmed_req, idem2 = asyncio.run(scenario())
    assert request.status == "pending" and idem is False
    assert still.status == "created"
    assert confirmed_req.status == "confirmed"
    assert confirmed_order.status == "cancelled"
    assert idem2 is False


def test_idempotency_key_replays_after_order_is_cancelled(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        first, _ = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        await store.confirm_cancellation(order_id=order.order_id, buyer_id="b1")
        replay, idem = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        return first, replay, idem

    first, replay, idem = asyncio.run(scenario())
    assert idem is True
    assert replay.request_id == first.request_id


def test_new_request_supersedes_old_pending(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        first, idem1 = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        second, idem2 = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k2"
        )
        latest = await store.latest_cancellation(order_id=order.order_id)
        events = await store.events(subject_type="order", subject_id=order.order_id)
        return first, second, latest, idem1, idem2, [e.event for e in events]

    first, second, latest, idem1, idem2, events = asyncio.run(scenario())
    assert idem1 is False and idem2 is False
    assert second.request_id != first.request_id
    assert latest.request_id == second.request_id
    assert events == [
        "order_created",
        "cancellation_requested",
        "cancellation_superseded",
        "cancellation_requested",
    ]


def test_withdraw_keeps_order_created(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        withdrawn, idem = await store.withdraw_cancellation(order_id=order.order_id, buyer_id="b1")
        again, idem2 = await store.withdraw_cancellation(order_id=order.order_id, buyer_id="b1")
        return (
            withdrawn,
            idem,
            again,
            idem2,
            await store.get_order(order_id=order.order_id, buyer_id="b1"),
        )

    withdrawn, idem, again, idem2, order = asyncio.run(scenario())
    assert withdrawn.status == "withdrawn"
    assert idem is False and idem2 is True
    assert again.status == "withdrawn"
    assert order.status == "created"


def test_confirm_after_withdraw_raises(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        await store.withdraw_cancellation(order_id=order.order_id, buyer_id="b1")
        with pytest.raises(CancellationNotPending) as excinfo:
            await store.confirm_cancellation(order_id=order.order_id, buyer_id="b1")
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.current_status == "withdrawn"


def test_request_on_cancelled_order_raises(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        await store.confirm_cancellation(order_id=order.order_id, buyer_id="b1")
        with pytest.raises(OrderAlreadyCancelled):
            await store.request_cancellation(
                order_id=order.order_id, buyer_id="b1", idempotency_key="k2"
            )

    asyncio.run(scenario())


def test_confirm_cancellation_is_idempotent(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        order = await _order(store)
        await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        first_order, first_req, idem = await store.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        again_order, again_req, idem2 = await store.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return first_order, first_req, idem, again_order, again_req, idem2

    first_order, first_req, idem, again_order, again_req, idem2 = asyncio.run(scenario())
    assert first_order.status == "cancelled"
    assert again_order.order_id == first_order.order_id
    assert again_req.request_id == first_req.request_id
    assert idem is False and idem2 is True
