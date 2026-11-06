"""S10: durable product evidence so drafts survive S7 context compression."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from harbor.features.orders.evidence import EvidenceRecord, SqliteEvidenceStore

_NOW = datetime(2026, 11, 5, tzinfo=UTC)


def _record(price: str, age_days: int) -> EvidenceRecord:
    return EvidenceRecord(
        buyer_id="b1",
        shopping_session_id="s1",
        sku_id="P1-S1",
        product_id="P1",
        title="猫饮水器",
        unit_price=Decimal(price),
        currency="CNY",
        observed_at=(_NOW - timedelta(days=age_days)).isoformat(),
    )


def test_latest_observation_wins(tmp_path) -> None:
    async def scenario():
        store = SqliteEvidenceStore(tmp_path / "orders.sqlite3")
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("99.00", 1)])
        return await store.find_many(buyer_id="b1", shopping_session_id="s1", sku_ids=["P1-S1"])

    found = asyncio.run(scenario())["P1-S1"]
    assert found.unit_price == Decimal("99.00")


def test_evidence_is_session_scoped(tmp_path) -> None:
    async def scenario():
        store = SqliteEvidenceStore(tmp_path / "orders.sqlite3")
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        return await store.find_many(buyer_id="b1", shopping_session_id="s2", sku_ids=["P1-S1"])

    assert asyncio.run(scenario()) == {}


def test_prune_removes_only_expired_rows(tmp_path) -> None:
    now = [datetime(2026, 11, 5, tzinfo=UTC)]

    async def scenario():
        store = SqliteEvidenceStore(
            tmp_path / "orders.sqlite3", retention_days=30, clock=lambda: now[0]
        )
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        now[0] += timedelta(days=31)
        removed = await store.prune()
        return removed, await store.find_many(
            buyer_id="b1", shopping_session_id="s1", sku_ids=["P1-S1"]
        )

    removed, found = asyncio.run(scenario())
    assert removed == 1
    assert found == {}


def test_unknown_sku_is_absent(tmp_path) -> None:
    async def scenario():
        store = SqliteEvidenceStore(tmp_path / "orders.sqlite3")
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        return await store.find_many(buyer_id="b1", shopping_session_id="s1", sku_ids=["P9-S9"])

    assert asyncio.run(scenario()) == {}


def test_empty_find_makes_no_query(tmp_path) -> None:
    store = SqliteEvidenceStore(Path(tmp_path) / "orders.sqlite3")
    assert asyncio.run(store.find_many(buyer_id="b1", shopping_session_id="s1", sku_ids=[])) == {}
