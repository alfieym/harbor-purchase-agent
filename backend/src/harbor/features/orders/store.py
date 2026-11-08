"""S10: SQLite order storage.

The storage layer owns the state machines. Both the model-facing draft tool and
the buyer-facing confirm endpoints call into this one store, so the transitions
(and their idempotency rules) cannot drift between them.

Concurrency mirrors ``SqliteMemoryStore``: WAL for readers, ``BEGIN IMMEDIATE``
for writers, ``busy_timeout`` so two processes queue, and an in-process
``asyncio.Lock`` so a burst of tool calls does not stampede the file.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from harbor.features.orders.models import (
    CancellationRequest,
    DraftItem,
    NewDraftItem,
    Order,
    OrderDraft,
    OrderEvent,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_drafts (
    id                  TEXT PRIMARY KEY,
    buyer_id            TEXT NOT NULL,
    shopping_session_id TEXT NOT NULL,
    status              TEXT NOT NULL,
    currency            TEXT NOT NULL,
    total_amount        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drafts_one_pending
    ON order_drafts(buyer_id, shopping_session_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS order_draft_items (
    draft_id             TEXT NOT NULL,
    line_no              INTEGER NOT NULL,
    product_id           TEXT NOT NULL,
    sku_id               TEXT NOT NULL,
    title                TEXT NOT NULL,
    quantity             INTEGER NOT NULL,
    unit_price           TEXT NOT NULL,
    currency             TEXT NOT NULL,
    evidence_unit_price  TEXT NOT NULL,
    evidence_currency    TEXT NOT NULL,
    PRIMARY KEY (draft_id, line_no)
);

CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL UNIQUE,
    buyer_id            TEXT NOT NULL,
    shopping_session_id TEXT NOT NULL,
    status              TEXT NOT NULL,
    currency            TEXT NOT NULL,
    total_amount        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders(buyer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS order_events (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    event        TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    actor        TEXT NOT NULL,
    source       TEXT NOT NULL,
    reason       TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject
    ON order_events(subject_type, subject_id, created_at);

CREATE TABLE IF NOT EXISTS order_cancellation_requests (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    buyer_id        TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT,
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    UNIQUE (buyer_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cancel_one_pending
    ON order_cancellation_requests(order_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_cancel_order_created
    ON order_cancellation_requests(order_id, created_at DESC);
"""

_DRAFT_COLUMNS = (
    "id, buyer_id, shopping_session_id, status, currency, total_amount, created_at, updated_at"
)
_ORDER_COLUMNS = (
    "id, draft_id, buyer_id, shopping_session_id, status, currency, total_amount, "
    "created_at, updated_at"
)
_ITEM_COLUMNS = (
    "line_no, product_id, sku_id, title, quantity, unit_price, currency, "
    "evidence_unit_price, evidence_currency"
)
_EVENT_COLUMNS = (
    "id, subject_type, subject_id, event, from_status, to_status, actor, source, "
    "reason, metadata, created_at"
)
_CANCELLATION_COLUMNS = (
    "id, order_id, buyer_id, status, reason, idempotency_key, created_at, decided_at"
)

T = TypeVar("T")


class OrderStoreError(Exception):
    """An order-store read or write failed."""


class DraftNotPending(OrderStoreError):
    """The draft is no longer ``pending`` and cannot be decided again."""

    def __init__(self, current_status: str) -> None:
        self.current_status = current_status
        super().__init__(f"draft status is {current_status}, not pending")


class OrderAlreadyCancelled(OrderStoreError):
    """A cancellation was requested for an order that is already cancelled."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"order {order_id} is already cancelled")


class CancellationNotPending(OrderStoreError):
    """The latest cancellation request is withdrawn or superseded."""

    def __init__(self, current_status: str) -> None:
        self.current_status = current_status
        super().__init__(f"cancellation status is {current_status}, not pending")


class NoCancellationRequest(OrderStoreError):
    """The order has no cancellation request to decide."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _DraftRow:
    """A raw draft row plus its items, so parsing does not re-query per field."""

    def __init__(self, row: tuple[Any, ...], items: Sequence[DraftItem]) -> None:
        self.row = row
        self.items = items


def _draft_from(row: _DraftRow) -> OrderDraft:
    return OrderDraft(
        draft_id=row.row[0],
        buyer_id=row.row[1],
        shopping_session_id=row.row[2],
        status=row.row[3],
        currency=row.row[4],
        total=Decimal(row.row[5]),
        items=tuple(row.items),
        created_at=row.row[6],
        updated_at=row.row[7],
    )


def _order_from(row: tuple[Any, ...], items: Sequence[DraftItem]) -> Order:
    return Order(
        order_id=row[0],
        draft_id=row[1],
        buyer_id=row[2],
        shopping_session_id=row[3],
        status=row[4],
        currency=row[5],
        total=Decimal(row[6]),
        items=tuple(items),
        created_at=row[7],
        updated_at=row[8],
    )


def _item_from(row: tuple[Any, ...]) -> DraftItem:
    return DraftItem(
        line_no=int(row[0]),
        product_id=row[1],
        sku_id=row[2],
        title=row[3],
        quantity=int(row[4]),
        unit_price=Decimal(row[5]),
        currency=row[6],
        evidence_unit_price=Decimal(row[7]),
        evidence_currency=row[8],
    )


def _event_from(row: tuple[Any, ...]) -> OrderEvent:
    return OrderEvent(
        event_id=row[0],
        subject_type=row[1],
        subject_id=row[2],
        event=row[3],
        from_status=row[4],
        to_status=row[5],
        actor=row[6],
        source=row[7],
        reason=row[8],
        metadata=row[9],
        created_at=row[10],
    )


def _cancellation_from(row: tuple[Any, ...]) -> CancellationRequest:
    return CancellationRequest(
        request_id=row[0],
        order_id=row[1],
        buyer_id=row[2],
        status=row[3],
        reason=row[4],
        idempotency_key=row[5],
        created_at=row[6],
        decided_at=row[7],
    )


def _same_items(existing: Sequence[DraftItem], incoming: Sequence[NewDraftItem]) -> bool:
    """Item identity for the draft replay rule: same SKUs and quantities."""
    existing_key = sorted((item.product_id, item.sku_id, item.quantity) for item in existing)
    incoming_key = sorted((item.product_id, item.sku_id, item.quantity) for item in incoming)
    return existing_key == incoming_key


class SqliteOrderStore:
    """Durable drafts, orders, and their state transitions."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._init_schema()

    # -- public async API -------------------------------------------------

    def ping(self) -> None:
        """Prove the database is reachable and its schema still exists.

        ``SELECT 1`` alone would answer even after the file was deleted (SQLite
        recreates an empty one), so the probe reads a real table: a vanished
        schema raises instead of reporting healthy.
        """
        conn = self._connect()
        try:
            conn.execute("SELECT 1 FROM order_drafts LIMIT 1").fetchall()
        finally:
            conn.close()

    async def create_draft(
        self,
        *,
        buyer_id: str,
        shopping_session_id: str,
        currency: str,
        items: Sequence[NewDraftItem],
    ) -> tuple[OrderDraft, bool]:
        """Create a pending draft, superseding the session's previous one.

        An identical pending draft (same SKUs/quantities and currency) is
        replayed instead, so a model retry does not void its own draft.
        """

        def operation(conn: sqlite3.Connection) -> tuple[OrderDraft, bool]:
            now = _now()
            pending_row = conn.execute(
                f"SELECT {_DRAFT_COLUMNS} FROM order_drafts "
                "WHERE buyer_id = ? AND shopping_session_id = ? AND status = 'pending'",
                (buyer_id, shopping_session_id),
            ).fetchone()
            if pending_row is not None:
                pending = _draft_from(
                    _DraftRow(pending_row, self._items_for_draft(conn, pending_row[0]))
                )
                if pending.currency == currency and _same_items(pending.items, items):
                    return pending, True
                conn.execute(
                    "UPDATE order_drafts SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, pending.draft_id),
                )
                self._insert_event(
                    conn,
                    subject_type="draft",
                    subject_id=pending.draft_id,
                    event="draft_superseded",
                    from_status="pending",
                    to_status="superseded",
                    actor="model",
                    source="tool",
                )

            draft_id = _new_id("drf")
            total = sum((item.unit_price * item.quantity for item in items), Decimal("0"))
            conn.execute(
                "INSERT INTO order_drafts "
                "(id, buyer_id, shopping_session_id, status, currency, total_amount, "
                "created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (draft_id, buyer_id, shopping_session_id, currency, str(total), now, now),
            )
            persisted: list[DraftItem] = []
            for line_no, item in enumerate(items, start=1):
                conn.execute(
                    "INSERT INTO order_draft_items "
                    "(draft_id, line_no, product_id, sku_id, title, quantity, unit_price, "
                    "currency, evidence_unit_price, evidence_currency) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        draft_id,
                        line_no,
                        item.product_id,
                        item.sku_id,
                        item.title,
                        item.quantity,
                        str(item.unit_price),
                        item.currency,
                        str(item.evidence_unit_price),
                        item.evidence_currency,
                    ),
                )
                persisted.append(
                    DraftItem(
                        line_no=line_no,
                        product_id=item.product_id,
                        sku_id=item.sku_id,
                        title=item.title,
                        quantity=item.quantity,
                        unit_price=item.unit_price,
                        currency=item.currency,
                        evidence_unit_price=item.evidence_unit_price,
                        evidence_currency=item.evidence_currency,
                    )
                )
            self._insert_event(
                conn,
                subject_type="draft",
                subject_id=draft_id,
                event="draft_created",
                from_status=None,
                to_status="pending",
                actor="model",
                source="tool",
            )
            draft = OrderDraft(
                draft_id=draft_id,
                buyer_id=buyer_id,
                shopping_session_id=shopping_session_id,
                status="pending",
                currency=currency,
                total=total,
                items=tuple(persisted),
                created_at=now,
                updated_at=now,
            )
            return draft, False

        return await self._locked_write(operation)

    async def get_draft(self, draft_id: str) -> OrderDraft | None:
        def operation(conn: sqlite3.Connection) -> OrderDraft | None:
            row = conn.execute(
                f"SELECT {_DRAFT_COLUMNS} FROM order_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                return None
            return _draft_from(_DraftRow(row, self._items_for_draft(conn, draft_id)))

        return await asyncio.to_thread(self._read_sync, operation)

    async def get_pending_draft(
        self, *, buyer_id: str, shopping_session_id: str
    ) -> OrderDraft | None:
        def operation(conn: sqlite3.Connection) -> OrderDraft | None:
            row = conn.execute(
                f"SELECT {_DRAFT_COLUMNS} FROM order_drafts "
                "WHERE buyer_id = ? AND shopping_session_id = ? AND status = 'pending'",
                (buyer_id, shopping_session_id),
            ).fetchone()
            if row is None:
                return None
            return _draft_from(_DraftRow(row, self._items_for_draft(conn, row[0])))

        return await asyncio.to_thread(self._read_sync, operation)

    async def confirm_draft(self, *, draft_id: str, buyer_id: str) -> tuple[Order, bool] | None:
        """Create the order for a pending draft; ``None`` means wrong buyer/id."""

        def operation(conn: sqlite3.Connection) -> tuple[Order, bool] | None:
            row = conn.execute(
                f"SELECT {_DRAFT_COLUMNS} FROM order_drafts WHERE id = ? AND buyer_id = ?",
                (draft_id, buyer_id),
            ).fetchone()
            if row is None:
                return None
            draft = _draft_from(_DraftRow(row, self._items_for_draft(conn, draft_id)))
            if draft.status == "confirmed":
                order_row = conn.execute(
                    f"SELECT {_ORDER_COLUMNS} FROM orders WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if order_row is None:  # pragma: no cover — invariant guard
                    raise OrderStoreError("confirmed draft has no order")
                return _order_from(order_row, draft.items), True
            if draft.status != "pending":
                raise DraftNotPending(draft.status)

            now = _now()
            order_id = _new_id("ord")
            conn.execute(
                "INSERT INTO orders "
                "(id, draft_id, buyer_id, shopping_session_id, status, currency, "
                "total_amount, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?)",
                (
                    order_id,
                    draft.draft_id,
                    draft.buyer_id,
                    draft.shopping_session_id,
                    draft.currency,
                    str(draft.total),
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE order_drafts SET status = 'confirmed', updated_at = ? WHERE id = ?",
                (now, draft.draft_id),
            )
            self._insert_event(
                conn,
                subject_type="draft",
                subject_id=draft.draft_id,
                event="draft_confirmed",
                from_status="pending",
                to_status="confirmed",
                actor="buyer",
                source="app",
            )
            self._insert_event(
                conn,
                subject_type="order",
                subject_id=order_id,
                event="order_created",
                from_status=None,
                to_status="created",
                actor="buyer",
                source="app",
            )
            order = Order(
                order_id=order_id,
                draft_id=draft.draft_id,
                buyer_id=draft.buyer_id,
                shopping_session_id=draft.shopping_session_id,
                status="created",
                currency=draft.currency,
                total=draft.total,
                items=draft.items,
                created_at=now,
                updated_at=now,
            )
            return order, False

        return await self._locked_write(operation)

    async def cancel_draft(self, *, draft_id: str, buyer_id: str) -> tuple[OrderDraft, bool] | None:
        """Void a pending draft; cancelling an already-cancelled draft is a no-op."""

        def operation(conn: sqlite3.Connection) -> tuple[OrderDraft, bool] | None:
            row = conn.execute(
                f"SELECT {_DRAFT_COLUMNS} FROM order_drafts WHERE id = ? AND buyer_id = ?",
                (draft_id, buyer_id),
            ).fetchone()
            if row is None:
                return None
            draft = _draft_from(_DraftRow(row, self._items_for_draft(conn, draft_id)))
            if draft.status == "cancelled":
                return draft, True
            if draft.status != "pending":
                raise DraftNotPending(draft.status)

            now = _now()
            conn.execute(
                "UPDATE order_drafts SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, draft_id),
            )
            self._insert_event(
                conn,
                subject_type="draft",
                subject_id=draft_id,
                event="draft_cancelled",
                from_status="pending",
                to_status="cancelled",
                actor="buyer",
                source="app",
            )
            return replace(draft, status="cancelled", updated_at=now), False

        return await self._locked_write(operation)

    async def get_order(self, *, order_id: str, buyer_id: str) -> Order | None:
        def operation(conn: sqlite3.Connection) -> Order | None:
            row = conn.execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = ? AND buyer_id = ?",
                (order_id, buyer_id),
            ).fetchone()
            if row is None:
                return None
            return _order_from(row, self._items_for_draft(conn, row[1]))

        return await asyncio.to_thread(self._read_sync, operation)

    async def request_cancellation(
        self,
        *,
        order_id: str,
        buyer_id: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> tuple[CancellationRequest, bool] | None:
        """Open a pending cancellation request; the order stays ``created``.

        The idempotency lookup comes before the order-state check: replaying the
        same key after the order was cancelled returns the original request
        instead of raising ``OrderAlreadyCancelled``.
        """

        def operation(conn: sqlite3.Connection) -> tuple[CancellationRequest, bool] | None:
            replay_row = conn.execute(
                f"SELECT {_CANCELLATION_COLUMNS} FROM order_cancellation_requests "
                "WHERE buyer_id = ? AND idempotency_key = ?",
                (buyer_id, idempotency_key),
            ).fetchone()
            if replay_row is not None:
                return _cancellation_from(replay_row), True

            order_row = conn.execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = ? AND buyer_id = ?",
                (order_id, buyer_id),
            ).fetchone()
            if order_row is None:
                return None
            if order_row[4] == "cancelled":
                raise OrderAlreadyCancelled(order_id)

            now = _now()
            pending = self._latest_cancellation_row(conn, order_id, status="pending")
            if pending is not None:
                conn.execute(
                    "UPDATE order_cancellation_requests "
                    "SET status = 'superseded', decided_at = ? WHERE id = ?",
                    (now, pending.request_id),
                )
                self._insert_event(
                    conn,
                    subject_type="order",
                    subject_id=order_id,
                    event="cancellation_superseded",
                    from_status="pending",
                    to_status="superseded",
                    actor="buyer",
                    source="app",
                )

            request_id = _new_id("can")
            conn.execute(
                "INSERT INTO order_cancellation_requests "
                "(id, order_id, buyer_id, status, reason, idempotency_key, created_at, "
                "decided_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL)",
                (request_id, order_id, buyer_id, reason, idempotency_key, now),
            )
            self._insert_event(
                conn,
                subject_type="order",
                subject_id=order_id,
                event="cancellation_requested",
                from_status=None,
                to_status="pending",
                actor="buyer",
                source="app",
            )
            return (
                CancellationRequest(
                    request_id=request_id,
                    order_id=order_id,
                    buyer_id=buyer_id,
                    status="pending",
                    reason=reason,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    decided_at=None,
                ),
                False,
            )

        return await self._locked_write(operation)

    async def confirm_cancellation(
        self, *, order_id: str, buyer_id: str
    ) -> tuple[Order, CancellationRequest, bool] | None:
        """The human gate for cancellation: order becomes ``cancelled`` here."""

        def operation(
            conn: sqlite3.Connection,
        ) -> tuple[Order, CancellationRequest, bool] | None:
            order_row = conn.execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = ? AND buyer_id = ?",
                (order_id, buyer_id),
            ).fetchone()
            if order_row is None:
                return None
            request = self._latest_cancellation_row(conn, order_id)
            if request is None:
                raise NoCancellationRequest(f"order {order_id} has no cancellation request")
            order = _order_from(order_row, self._items_for_draft(conn, order_row[1]))
            if order.status == "cancelled" or request.status == "confirmed":
                return order, request, True
            if request.status != "pending":
                raise CancellationNotPending(request.status)

            now = _now()
            conn.execute(
                "UPDATE order_cancellation_requests "
                "SET status = 'confirmed', decided_at = ? WHERE id = ?",
                (now, request.request_id),
            )
            conn.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, order_id),
            )
            self._insert_event(
                conn,
                subject_type="order",
                subject_id=order_id,
                event="cancellation_confirmed",
                from_status="pending",
                to_status="confirmed",
                actor="buyer",
                source="app",
            )
            self._insert_event(
                conn,
                subject_type="order",
                subject_id=order_id,
                event="order_cancelled",
                from_status="created",
                to_status="cancelled",
                actor="buyer",
                source="app",
            )
            return (
                replace(order, status="cancelled", updated_at=now),
                replace(request, status="confirmed", decided_at=now),
                False,
            )

        return await self._locked_write(operation)

    async def withdraw_cancellation(
        self, *, order_id: str, buyer_id: str
    ) -> tuple[CancellationRequest, bool] | None:
        """The buyer changes their mind; the order stays ``created``."""

        def operation(conn: sqlite3.Connection) -> tuple[CancellationRequest, bool] | None:
            order_row = conn.execute(
                "SELECT id FROM orders WHERE id = ? AND buyer_id = ?",
                (order_id, buyer_id),
            ).fetchone()
            if order_row is None:
                return None
            request = self._latest_cancellation_row(conn, order_id)
            if request is None:
                # The order exists and is the buyer's, so this is a state
                # conflict (409), not a missing resource (S10 spec §5.3).
                raise NoCancellationRequest(f"order {order_id} has no cancellation request")
            if request.status == "withdrawn":
                return request, True
            if request.status != "pending":
                raise CancellationNotPending(request.status)

            now = _now()
            conn.execute(
                "UPDATE order_cancellation_requests "
                "SET status = 'withdrawn', decided_at = ? WHERE id = ?",
                (now, request.request_id),
            )
            self._insert_event(
                conn,
                subject_type="order",
                subject_id=order_id,
                event="cancellation_withdrawn",
                from_status="pending",
                to_status="withdrawn",
                actor="buyer",
                source="app",
            )
            return replace(request, status="withdrawn", decided_at=now), False

        return await self._locked_write(operation)

    async def latest_cancellation(self, *, order_id: str) -> CancellationRequest | None:
        def operation(conn: sqlite3.Connection) -> CancellationRequest | None:
            return self._latest_cancellation_row(conn, order_id)

        return await asyncio.to_thread(self._read_sync, operation)

    async def events(self, *, subject_type: str, subject_id: str) -> list[OrderEvent]:
        def operation(conn: sqlite3.Connection) -> list[OrderEvent]:
            rows = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM order_events "
                "WHERE subject_type = ? AND subject_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (subject_type, subject_id),
            ).fetchall()
            return [_event_from(row) for row in rows]

        return await asyncio.to_thread(self._read_sync, operation)

    # -- internals --------------------------------------------------------

    def _items_for_draft(self, conn: sqlite3.Connection, draft_id: str) -> list[DraftItem]:
        rows = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM order_draft_items "
            "WHERE draft_id = ? ORDER BY line_no ASC",
            (draft_id,),
        ).fetchall()
        return [_item_from(row) for row in rows]

    def _latest_cancellation_row(
        self, conn: sqlite3.Connection, order_id: str, *, status: str | None = None
    ) -> CancellationRequest | None:
        sql = f"SELECT {_CANCELLATION_COLUMNS} FROM order_cancellation_requests WHERE order_id = ?"
        params: list[Any] = [order_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return None if row is None else _cancellation_from(row)

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        subject_type: str,
        subject_id: str,
        event: str,
        from_status: str | None,
        to_status: str | None,
        actor: str,
        source: str,
        reason: str | None = None,
        metadata: str = "{}",
    ) -> None:
        conn.execute(
            "INSERT INTO order_events "
            "(id, subject_type, subject_id, event, from_status, to_status, actor, source, "
            "reason, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id("evt"),
                subject_type,
                subject_id,
                event,
                from_status,
                to_status,
                actor,
                source,
                reason,
                metadata,
                _now(),
            ),
        )

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
        except OrderStoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise OrderStoreError(str(exc)) from exc
        finally:
            conn.close()

    def _read_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            return operation(conn)
        finally:
            conn.close()
