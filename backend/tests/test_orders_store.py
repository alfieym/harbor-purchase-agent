"""S10: SQLite order drafts, orders, and the write/decision state machines."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.orders.models import NewDraftItem
from harbor.features.orders.store import DraftNotPending, SqliteOrderStore


def _store(tmp_path: Path) -> SqliteOrderStore:
    return SqliteOrderStore(tmp_path / "orders.sqlite3")


def _item(sku: str = "P1-S1", qty: int = 1, price: str = "89.00") -> NewDraftItem:
    return NewDraftItem(
        product_id="P1",
        sku_id=sku,
        title="猫饮水器",
        quantity=qty,
        unit_price=Decimal(price),
        currency="CNY",
        evidence_unit_price=Decimal(price),
        evidence_currency="CNY",
    )


def test_new_draft_supersedes_the_previous_pending_one(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        first, replayed = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        second, replayed2 = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item(qty=2)]
        )
        return first, second, replayed, replayed2, await store.get_draft(first.draft_id)

    first, second, replayed, replayed2, old = asyncio.run(scenario())
    assert replayed is False and replayed2 is False
    assert second.draft_id != first.draft_id
    assert old.status == "superseded"


def test_identical_pending_draft_is_replayed(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        first, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        second, replayed = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        return first, second, replayed

    first, second, replayed = asyncio.run(scenario())
    assert replayed is True
    assert second.draft_id == first.draft_id


def test_draft_does_not_create_an_order(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        return draft, await store.get_order(order_id="ord_does_not_exist", buyer_id="b1")

    draft, order = asyncio.run(scenario())
    assert draft.status == "pending"
    assert order is None


def test_confirm_creates_order_and_is_idempotent(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item(qty=2)]
        )
        order, idem = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        again, idem2 = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        return (
            draft,
            order,
            again,
            idem,
            idem2,
            await store.events(subject_type="order", subject_id=order.order_id),
            await store.events(subject_type="draft", subject_id=draft.draft_id),
        )

    draft, order, again, idem, idem2, order_events, draft_events = asyncio.run(scenario())
    assert order.total == Decimal("178.00")
    assert order.items[0].quantity == 2
    assert idem is False and idem2 is True
    assert again.order_id == order.order_id
    assert [e.event for e in order_events] == ["order_created"]
    assert [e.event for e in draft_events] == ["draft_created", "draft_confirmed"]


def test_superseded_draft_cannot_be_confirmed(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        old, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item(qty=2)]
        )
        with pytest.raises(DraftNotPending) as excinfo:
            await store.confirm_draft(draft_id=old.draft_id, buyer_id="b1")
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.current_status == "superseded"


def test_other_buyer_cannot_confirm_the_draft(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        return draft, await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b2")

    draft, result = asyncio.run(scenario())
    assert draft.status == "pending"
    assert result is None


def test_cancel_draft_is_idempotent(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        first, idem = await store.cancel_draft(draft_id=draft.draft_id, buyer_id="b1")
        again, idem2 = await store.cancel_draft(draft_id=draft.draft_id, buyer_id="b1")
        with pytest.raises(DraftNotPending):
            await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        return first, again, idem, idem2

    first, again, idem, idem2 = asyncio.run(scenario())
    assert first.status == "cancelled"
    assert idem is False and idem2 is True
    assert again.status == "cancelled"


def test_concurrent_confirm_creates_one_order(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        results = await asyncio.gather(
            store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"),
            store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"),
        )
        return draft, results

    draft, results = asyncio.run(scenario())
    assert results[0][0].order_id == results[1][0].order_id
    assert sum(1 for _, idem in results if not idem) == 1
