"""S10: durable price evidence for order drafts.

S7 compression shortens the agent's context, so a search hit the model saw ten
turns ago may be gone when the buyer finally says "下这个单". Recording each
observation here — keyed by buyer, session, and SKU — lets the draft tool verify
a price without asking the model to remember it.

Retention is a hard 30 days by default: a price observed last month is not
evidence for today's purchase. Pruning runs at startup and at most once per day
after that, on the first write.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_evidence (
    buyer_id            TEXT NOT NULL,
    shopping_session_id TEXT NOT NULL,
    sku_id              TEXT NOT NULL,
    product_id          TEXT,
    title               TEXT NOT NULL,
    unit_price          TEXT NOT NULL,
    currency            TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    PRIMARY KEY (buyer_id, shopping_session_id, sku_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_observed ON product_evidence(observed_at);
"""

_COLUMNS = (
    "buyer_id, shopping_session_id, sku_id, product_id, title, unit_price, currency, observed_at"
)

T = TypeVar("T")


class EvidenceStoreError(Exception):
    """An evidence-store read or write failed."""


@dataclass(frozen=True)
class EvidenceRecord:
    """One price observation.

    ``product_id`` is ``None`` when the observation came from the S7 compression
    appendix, which protects SKU, title, and price but not the product id.
    """

    buyer_id: str
    shopping_session_id: str
    sku_id: str
    product_id: str | None
    title: str
    unit_price: Decimal
    currency: str
    observed_at: str


def _record_from(row: tuple[Any, ...]) -> EvidenceRecord:
    return EvidenceRecord(
        buyer_id=row[0],
        shopping_session_id=row[1],
        sku_id=row[2],
        product_id=row[3],
        title=row[4],
        unit_price=Decimal(row[5]),
        currency=row[6],
        observed_at=row[7],
    )


class SqliteEvidenceStore:
    """Records and looks up price observations per buyer/session/SKU."""

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._retention = timedelta(days=retention_days)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._last_prune_date: date | None = None
        self._init_schema()
        self._write_sync(lambda conn: self._prune_conn(conn, self._clock()))
        self._last_prune_date = self._clock().date()

    async def record(
        self,
        *,
        buyer_id: str,
        shopping_session_id: str,
        records: Sequence[EvidenceRecord],
    ) -> None:
        """Upsert observations for this buyer and session.

        The keyword scope wins over each record's own fields: a tool cannot
        write evidence into another session by putting a different id in a hit.
        """
        if not records:
            return

        rows = [
            (
                buyer_id,
                shopping_session_id,
                record.sku_id,
                record.product_id,
                record.title,
                str(record.unit_price),
                record.currency,
                record.observed_at,
            )
            for record in records
        ]

        def operation(conn: sqlite3.Connection) -> None:
            now = self._clock()
            self._maybe_prune(conn, now)
            conn.executemany(
                "INSERT INTO product_evidence "
                f"({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(buyer_id, shopping_session_id, sku_id) DO UPDATE SET "
                "product_id = excluded.product_id, "
                "title = excluded.title, "
                "unit_price = excluded.unit_price, "
                "currency = excluded.currency, "
                "observed_at = excluded.observed_at",
                rows,
            )

        await self._locked_write(operation)

    async def find_many(
        self,
        *,
        buyer_id: str,
        shopping_session_id: str,
        sku_ids: Sequence[str],
    ) -> dict[str, EvidenceRecord]:
        if not sku_ids:
            return {}

        placeholders = ", ".join("?" for _ in sku_ids)

        def operation(conn: sqlite3.Connection) -> dict[str, EvidenceRecord]:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM product_evidence "
                "WHERE buyer_id = ? AND shopping_session_id = ? "
                f"AND sku_id IN ({placeholders})",
                (buyer_id, shopping_session_id, *sku_ids),
            ).fetchall()
            return {record.sku_id: record for record in map(_record_from, rows)}

        return await asyncio.to_thread(self._read_sync, operation)

    async def prune(self) -> int:
        def operation(conn: sqlite3.Connection) -> int:
            now = self._clock()
            removed = self._prune_conn(conn, now)
            self._last_prune_date = now.date()
            return removed

        return await self._locked_write(operation)

    # -- internals --------------------------------------------------------

    def _maybe_prune(self, conn: sqlite3.Connection, now: datetime) -> None:
        if self._last_prune_date == now.date():
            return
        self._prune_conn(conn, now)
        self._last_prune_date = now.date()

    def _prune_conn(self, conn: sqlite3.Connection, now: datetime) -> int:
        cutoff = (now - self._retention).isoformat()
        cursor = conn.execute("DELETE FROM product_evidence WHERE observed_at < ?", (cutoff,))
        return cursor.rowcount

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    async def _locked_write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:
            return await asyncio.to_thread(self._write_sync, operation)

    def _write_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except EvidenceStoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise EvidenceStoreError(str(exc)) from exc
        finally:
            conn.close()

    def _read_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            return operation(conn)
        finally:
            conn.close()
