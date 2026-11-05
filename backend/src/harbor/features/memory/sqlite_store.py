"""S8: SQLite-backed long-term memory (one row = text + vector, one transaction).

Why SQLite: preferences are small records written often. The design requirement is
that a record's text and its embedding can never diverge — so they are inserted
(and updated) in the *same statement*, inside the *same transaction*. A row is
either complete or absent.

Concurrency: WAL lets readers run while one writer works; ``BEGIN IMMEDIATE``
takes the write lock up front so two processes queue instead of failing late,
and ``busy_timeout`` makes them wait. In-process, an ``asyncio.Lock`` serializes
writes so a burst of tool calls does not stampede the file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from harbor.features.memory.vector import cosine, pack, unpack

DEFAULT_LIST_LIMIT = 24
DEFAULT_SEARCH_LIMIT = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    buyer_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    text         TEXT NOT NULL,
    vector       BLOB NOT NULL,
    vector_model TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- Semantic key used to collapse restatements of the same preference.
    -- Nullable so rows written before S8 can be migrated without a rewrite;
    -- SQLite treats NULLs as distinct in a UNIQUE index.
    text_key     TEXT,
    -- Optimistic concurrency: every update bumps this, and a caller that read
    -- the row at version N must send N back or the write is refused (S8-4).
    version      INTEGER NOT NULL DEFAULT 1,
    -- JSON array of material tags; only ``kind='dislike'`` rows use it, but the
    -- column is NOT NULL so every row has one representation.
    material_tags TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_memories_buyer_kind ON memories(buyer_id, kind);
"""

_COLUMNS = (
    "id, buyer_id, kind, scope, text, vector, vector_model, created_at, updated_at, "
    "text_key, version, material_tags"
)
T = TypeVar("T")


class MemoryStoreError(Exception):
    """A read or write to the long-term memory store failed."""


class MemoryVersionConflict(MemoryStoreError):
    """The caller's ``expected_version`` is stale; nothing was written."""

    def __init__(self, current: MemoryRecord) -> None:
        self.current = current
        super().__init__(f"版本冲突：当前 version={current.version}")


class MemoryLimitReached(MemoryStoreError):
    """The buyer already has the maximum number of records of this kind."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"记忆数量已达上限 {limit}")


@dataclass(frozen=True)
class MemoryRecord:
    """One durable memory: its text and embedding always travel together."""

    id: str
    buyer_id: str
    kind: str
    scope: str
    text: str
    vector: list[float]
    vector_model: str
    created_at: str
    updated_at: str
    version: int = 1
    material_tags: tuple[str, ...] = ()


# A module-level alias, not a bare ``list[...]``: inside the class body the
# name ``list`` resolves to the ``list`` method, not the builtin.
MemoryRecords = list[MemoryRecord]
ScoredMemoryRecords = list[tuple[MemoryRecord, float]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row: tuple[Any, ...]) -> MemoryRecord:
    return MemoryRecord(
        id=row[0],
        buyer_id=row[1],
        kind=row[2],
        scope=row[3],
        text=row[4],
        vector=unpack(row[5]),
        vector_model=row[6],
        created_at=row[7],
        updated_at=row[8],
        version=int(row[10]),
        material_tags=tuple(json.loads(row[11] or "[]")),
    )


def _insert_row(conn: sqlite3.Connection, record: MemoryRecord, text_key: str | None) -> None:
    """The single statement that writes text *and* vector (S8-4)."""
    conn.execute(
        f"INSERT INTO memories ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.id,
            record.buyer_id,
            record.kind,
            record.scope,
            record.text,
            pack(record.vector),
            record.vector_model,
            record.created_at,
            record.updated_at,
            text_key,
            record.version,
            json.dumps(list(record.material_tags), ensure_ascii=False),
        ),
    )


class SqliteMemoryStore:
    """Durable memories keyed by ``buyer_id``; S8 writes ``kind='preference'``."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._init_schema()

    # -- public async API -------------------------------------------------

    async def add(
        self,
        *,
        buyer_id: str,
        text: str,
        vector: Sequence[float],
        vector_model: str,
        kind: str = "preference",
        text_key: str | None = None,
        material_tags: Sequence[str] = (),
        max_count: int | None = None,
    ) -> MemoryRecord:
        record, _ = await self.add_or_get(
            buyer_id=buyer_id,
            text=text,
            vector=vector,
            vector_model=vector_model,
            kind=kind,
            text_key=text_key,
            material_tags=material_tags,
            max_count=max_count,
        )
        return record

    async def add_or_get(
        self,
        *,
        buyer_id: str,
        text: str,
        vector: Sequence[float],
        vector_model: str,
        kind: str = "preference",
        text_key: str | None = None,
        material_tags: Sequence[str] = (),
        max_count: int | None = None,
    ) -> tuple[MemoryRecord, bool]:
        """Insert unless ``(buyer_id, kind, text_key)`` already exists.

        The lookup runs inside the same ``BEGIN IMMEDIATE`` transaction as the
        insert, so two sessions (or two processes) adding the same preference
        cannot both create a row. Returns ``(record, created)``; when ``created``
        is ``False`` the record is the one already stored.

        ``max_count`` caps how many rows of this ``kind`` one buyer may hold.
        The count is read inside the same transaction, so a burst of concurrent
        adds cannot slip past the cap (S8-5). A restatement of an existing row
        still dedupes at the cap; only a genuinely new record is rejected.
        """
        now = _now()
        record = MemoryRecord(
            id=f"pref_{uuid.uuid4().hex[:8]}",
            buyer_id=buyer_id,
            kind=kind,
            scope="long_term",
            text=text,
            vector=list(vector),
            vector_model=vector_model,
            created_at=now,
            updated_at=now,
            material_tags=tuple(material_tags),
        )

        def operation(conn: sqlite3.Connection) -> tuple[MemoryRecord, bool]:
            if text_key is not None:
                existing = self._select_by_text_key(conn, buyer_id, kind, text_key)
                if existing is not None:
                    return _row_to_record(existing), False
            if max_count is not None and self._count_in(conn, buyer_id, kind) >= max_count:
                raise MemoryLimitReached(max_count)
            _insert_row(conn, record, text_key)
            return record, True

        return await self._locked_write(operation)

    async def update_text(
        self,
        *,
        buyer_id: str,
        memory_id: str,
        text: str,
        vector: Sequence[float],
        vector_model: str,
        text_key: str | None = None,
        expected_version: int | None = None,
    ) -> MemoryRecord | None:
        def operation(conn: sqlite3.Connection) -> MemoryRecord | None:
            row = self._select_one(conn, buyer_id, memory_id)
            if row is None:
                return None
            record = _row_to_record(row)
            # Checked inside the write lock, so the version cannot change
            # between the comparison and the UPDATE.
            if expected_version is not None and record.version != expected_version:
                raise MemoryVersionConflict(record)
            updated_at = _now()
            conn.execute(
                "UPDATE memories SET text = ?, vector = ?, vector_model = ?, updated_at = ?, "
                "text_key = ?, version = version + 1 WHERE id = ? AND buyer_id = ?",
                (
                    text,
                    pack(list(vector)),
                    vector_model,
                    updated_at,
                    text_key if text_key is not None else text,
                    memory_id,
                    buyer_id,
                ),
            )
            return MemoryRecord(
                id=record.id,
                buyer_id=record.buyer_id,
                kind=record.kind,
                scope=record.scope,
                text=text,
                vector=list(vector),
                vector_model=vector_model,
                created_at=record.created_at,
                updated_at=updated_at,
                version=record.version + 1,
                material_tags=record.material_tags,
            )

        return await self._locked_write(operation)

    async def delete(self, *, buyer_id: str, memory_id: str) -> MemoryRecord | None:
        def operation(conn: sqlite3.Connection) -> MemoryRecord | None:
            row = self._select_one(conn, buyer_id, memory_id)
            if row is None:
                return None
            conn.execute(
                "DELETE FROM memories WHERE id = ? AND buyer_id = ?", (memory_id, buyer_id)
            )
            return _row_to_record(row)

        return await self._locked_write(operation)

    async def list(
        self,
        *,
        buyer_id: str,
        kind: str | None = "preference",
        limit: int | None = DEFAULT_LIST_LIMIT,
    ) -> MemoryRecords:
        """Newest first. ``kind=None`` returns preferences and dislikes together."""
        return await asyncio.to_thread(self._list_sync, buyer_id, kind, limit)

    async def get(self, *, buyer_id: str, memory_id: str) -> MemoryRecord | None:
        """One record by id, scoped to its buyer (an id alone cannot reach it)."""
        return await asyncio.to_thread(self._get_sync, buyer_id, memory_id)

    async def count(self, *, buyer_id: str, kind: str = "preference") -> int:
        return await asyncio.to_thread(self._count_sync, buyer_id, kind)

    async def search_similar(
        self,
        *,
        buyer_id: str,
        vector: Sequence[float],
        limit: int = DEFAULT_SEARCH_LIMIT,
        vector_model: str | None = None,
        kind: str = "preference",
    ) -> ScoredMemoryRecords:
        """Top-``limit`` records by cosine; a different vector model is skipped."""
        return await asyncio.to_thread(
            self._search_sync, buyer_id, list(vector), limit, vector_model, kind
        )

    # -- sync internals (run in a worker thread) --------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            if "text_key" not in columns:
                # Pre-S8 database: add the column without rewriting rows. Old
                # rows keep NULL, which SQLite's UNIQUE index treats as distinct.
                conn.execute("ALTER TABLE memories ADD COLUMN text_key TEXT")
            if "version" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "material_tags" not in columns:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN material_tags TEXT NOT NULL DEFAULT '[]'"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedupe "
                "ON memories(buyer_id, kind, text_key)"
            )
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
        except MemoryStoreError:
            # Conflict and cap are decisions, not storage failures: the caller
            # needs the exception type (and the current row) to answer 409 or
            # ``limit_reached``, so they are not flattened into MemoryStoreError.
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise MemoryStoreError(str(exc)) from exc
        finally:
            conn.close()

    @staticmethod
    def _select_by_text_key(
        conn: sqlite3.Connection, buyer_id: str, kind: str, text_key: str
    ) -> tuple[Any, ...] | None:
        cursor = conn.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE buyer_id = ? AND kind = ? AND text_key = ?",
            (buyer_id, kind, text_key),
        )
        return cursor.fetchone()

    @staticmethod
    def _select_one(
        conn: sqlite3.Connection, buyer_id: str, memory_id: str
    ) -> tuple[Any, ...] | None:
        cursor = conn.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE id = ? AND buyer_id = ?",
            (memory_id, buyer_id),
        )
        return cursor.fetchone()

    def _get_sync(self, buyer_id: str, memory_id: str) -> MemoryRecord | None:
        conn = self._connect()
        try:
            row = self._select_one(conn, buyer_id, memory_id)
            return _row_to_record(row) if row is not None else None
        finally:
            conn.close()

    def _list_sync(self, buyer_id: str, kind: str | None, limit: int | None) -> MemoryRecords:
        conn = self._connect()
        try:
            sql = f"SELECT {_COLUMNS} FROM memories WHERE buyer_id = ?"
            params: list[Any] = [buyer_id]
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            sql += " ORDER BY created_at DESC, rowid DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            return [_row_to_record(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _count_in(conn: sqlite3.Connection, buyer_id: str, kind: str) -> int:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE buyer_id = ? AND kind = ?", (buyer_id, kind)
        )
        return int(cursor.fetchone()[0])

    def _count_sync(self, buyer_id: str, kind: str) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE buyer_id = ? AND kind = ?",
                (buyer_id, kind),
            )
            return int(cursor.fetchone()[0])
        finally:
            conn.close()

    def _search_sync(
        self,
        buyer_id: str,
        vector: Sequence[float],
        limit: int,
        vector_model: str | None,
        kind: str,
    ) -> ScoredMemoryRecords:
        records = self._list_sync(buyer_id, kind, limit=None)
        if vector_model is not None:
            records = [record for record in records if record.vector_model == vector_model]
        scored = [(record, cosine(vector, record.vector)) for record in records]
        scored = [item for item in scored if item[1] > -1.0]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]
