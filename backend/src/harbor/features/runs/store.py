"""SQLite persistence for runs and their event logs (S14).

The ordering rule lives here: an event is written and given its sequence
number inside one ``BEGIN IMMEDIATE`` transaction, and only after that commit
does the manager broadcast it. A subscriber therefore never observes an event
that a refresh could not replay, and a crash cannot leave a gap.

One connection per operation and a process-wide lock mirror the S10 store:
SQLite serialises writers across processes, the lock keeps a single-process
burst from contending on the file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = ["RunExists", "RunNotFound", "RunRecord", "SqliteRunStore"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    thread_id           TEXT NOT NULL,
    buyer_id            TEXT NOT NULL,
    shopping_session_id TEXT NOT NULL,
    input_text          TEXT NOT NULL,
    status              TEXT NOT NULL,
    error               TEXT,
    owner               TEXT,
    heartbeat_at        TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    enqueued_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, created_at);

CREATE TABLE IF NOT EXISTS run_events (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- S15: one conversation at a time. An in-process ``asyncio.Lock`` cannot span
-- the API and the workers, so exclusivity lives in the one place both share.
CREATE TABLE IF NOT EXISTS session_leases (
    session_id TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    owner      TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Columns S15 added to a table S14 created. ``CREATE TABLE IF NOT EXISTS``
# leaves an existing table alone, so upgrades are applied one by one.
_S15_COLUMNS = {
    "owner": "TEXT",
    "heartbeat_at": "TEXT",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    "enqueued_at": "TEXT",
}


class RunNotFound(Exception):
    """The run id does not exist."""


class RunExists(Exception):
    """A run with this id was already created (client-supplied runId replay)."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    thread_id: str
    buyer_id: str
    shopping_session_id: str
    input_text: str
    status: str
    error: str | None
    created_at: str
    updated_at: str
    last_seq: int
    owner: str | None = None


class SqliteRunStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Injectable so lease expiry and requeue deadlines can be tested
        # without sleeping.
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    # ---------------------------------------------------------------- clock

    def _now(self) -> str:
        return self._clock().isoformat()

    def _cutoff(self, seconds: float) -> str:
        return (self._clock() - timedelta(seconds=seconds)).isoformat()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        present = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for column, decl in _S15_COLUMNS.items():
            if column not in present:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {decl}")
        # Rows that predate S15 have no enqueue time; their creation time is
        # the closest honest stand-in.
        conn.execute("UPDATE runs SET enqueued_at = created_at WHERE enqueued_at IS NULL")

    # ------------------------------------------------------------- plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _require(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return row

    # ---------------------------------------------------------------- writes

    def create_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        buyer_id: str,
        shopping_session_id: str,
        input_text: str,
        status: str = "queued",
    ) -> None:
        now = self._now()
        with self._lock, self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, thread_id, buyer_id, shopping_session_id,"
                    " input_text, status, error, enqueued_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (
                        run_id,
                        thread_id,
                        buyer_id,
                        shopping_session_id,
                        input_text,
                        status,
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RunExists(run_id) from exc

    def append_event(
        self,
        run_id: str,
        event: dict,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> int:
        """Persist one event and return its per-run sequence number.

        ``status`` is for terminal events: it commits the run's ending in the
        same transaction as the ending itself. The manager broadcasts right
        after this returns, so an observer that can see ``RUN_FINISHED`` can
        always read the matching status — no polling, no window.
        """
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as conn:
            # BEGIN IMMEDIATE takes the write lock before we read MAX(seq), so
            # two writers cannot both compute the same next number.
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._require(conn, run_id)
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                seq = int(row[0])
                now = self._now()
                conn.execute(
                    "INSERT INTO run_events (run_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, seq, payload, now),
                )
                if status is None:
                    conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (now, run_id))
                else:
                    conn.execute(
                        "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                        (status, error, now, run_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return seq

    def finish_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                (status, error, self._now(), run_id),
            )
            if cursor.rowcount == 0:
                raise RunNotFound(run_id)

    def interrupt_running(self) -> list[str]:
        """Mark runs left ``running`` by a previous process as interrupted.

        Their events stay intact; the manager appends the terminal ``RUN_ERROR``
        so a replay always ends in a terminal state.
        """
        with self._lock, self._connection() as conn:
            ids = [
                row["run_id"]
                for row in conn.execute(
                    "SELECT run_id FROM runs WHERE status = 'running' ORDER BY created_at, run_id"
                )
            ]
            if ids:
                conn.executemany(
                    "UPDATE runs SET status = 'interrupted', error = ?, updated_at = ?"
                    " WHERE run_id = ?",
                    [("server_restarted", self._now(), run_id) for run_id in ids],
                )
        return ids

    # ------------------------------------------------------ S15 queue, leases

    def try_claim(self, run_id: str, owner: str, *, lease_seconds: float = 90.0) -> bool:
        """Take a queued run for one worker; ``False`` means somebody else did.

        The duplicate-delivery defence is this single conditional ``UPDATE``:
        a message claimed twice executes once, because the second statement
        matches no row.
        """
        now = self._now()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'running', owner = ?, heartbeat_at = ?,"
                " updated_at = ? WHERE run_id = ? AND status = 'queued'",
                (owner, now, now, run_id),
            )
            return cursor.rowcount == 1

    def heartbeat(self, run_id: str, owner: str) -> bool:
        """Renew a lease; ``False`` means this worker no longer owns the run."""
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET heartbeat_at = ?, updated_at = ?"
                " WHERE run_id = ? AND owner = ? AND status = 'running'",
                (self._now(), self._now(), run_id, owner),
            )
            return cursor.rowcount == 1

    def cancel_requested(self, run_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row["cancel_requested"]) if row is not None else False

    def request_cancel(self, run_id: str) -> bool:
        """Cancel a run, or ask its worker to.

        Returns ``True`` when this call ended the run (it was still queued and
        nobody was executing it) and ``False`` when a worker owns it — in that
        case it has only been asked, because the terminal events must be
        written by the process that holds the event log's write order.
        """
        now = self._now()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'cancelled', updated_at = ?"
                " WHERE run_id = ? AND status = 'queued'",
                (now, run_id),
            )
            if cursor.rowcount == 1:
                return True
            conn.execute(
                "UPDATE runs SET cancel_requested = 1, updated_at = ?"
                " WHERE run_id = ? AND status = 'running'",
                (now, run_id),
            )
            return False

    def interrupt_stale(self, *, lease_seconds: float = 90.0) -> list[str]:
        """Interrupt runs whose worker stopped reporting in.

        A run that has started cannot be re-executed: its narration is already
        on someone's screen. The honest ending is ``interrupted``.
        """
        cutoff = self._cutoff(lease_seconds)
        with self._lock, self._connection() as conn:
            ids = [
                row["run_id"]
                for row in conn.execute(
                    "SELECT run_id FROM runs WHERE status = 'running'"
                    " AND (heartbeat_at IS NULL OR heartbeat_at < ?)"
                    " ORDER BY created_at, run_id",
                    (cutoff,),
                )
            ]
            if ids:
                conn.executemany(
                    "UPDATE runs SET status = 'interrupted', error = ?, updated_at = ?"
                    " WHERE run_id = ? AND status = 'running'",
                    [("worker_lost", self._now(), run_id) for run_id in ids],
                )
        return ids

    def stale_queued(self, *, seconds: float = 15.0) -> list[str]:
        """Runs whose enqueue never landed, or whose worker was not ready."""
        cutoff = self._cutoff(seconds)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status = 'queued'"
                " AND COALESCE(enqueued_at, created_at) < ? ORDER BY created_at, run_id",
                (cutoff,),
            ).fetchall()
        return [row["run_id"] for row in rows]

    def mark_enqueued(self, run_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE runs SET enqueued_at = ?, updated_at = ?"
                " WHERE run_id = ? AND status = 'queued'",
                (self._now(), self._now(), run_id),
            )

    def acquire_session(
        self, session_id: str, run_id: str, owner: str, *, ttl_seconds: float = 300.0
    ) -> bool:
        """One conversation at a time, across processes.

        The lease names the run that holds it, so a late release from a worker
        that has already lost the lease cannot free somebody else's.
        """
        expires_at = (self._clock() + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO session_leases (session_id, run_id, owner, expires_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET run_id = excluded.run_id,"
                " owner = excluded.owner, expires_at = excluded.expires_at"
                " WHERE session_leases.expires_at <= ?",
                (session_id, run_id, owner, expires_at, self._now()),
            )
            return cursor.rowcount == 1

    def renew_session(self, session_id: str, run_id: str, *, ttl_seconds: float = 300.0) -> bool:
        expires_at = (self._clock() + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE session_leases SET expires_at = ? WHERE session_id = ? AND run_id = ?",
                (expires_at, session_id, run_id),
            )
            return cursor.rowcount == 1

    def release_session(self, session_id: str, run_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "DELETE FROM session_leases WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            )

    def purge_expired_sessions(self) -> int:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM session_leases WHERE expires_at <= ?", (self._now(),)
            )
            return cursor.rowcount

    # ---------------------------------------------------------------- reads

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT r.*, COALESCE((SELECT MAX(seq) FROM run_events e"
                " WHERE e.run_id = r.run_id), 0) AS last_seq"
                " FROM runs r WHERE r.run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            buyer_id=row["buyer_id"],
            shopping_session_id=row["shopping_session_id"],
            input_text=row["input_text"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seq=int(row["last_seq"]),
            owner=row["owner"],
        )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[tuple[int, dict]]:
        with self._connection() as conn:
            self._require(conn, run_id)
            rows = conn.execute(
                "SELECT seq, payload FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [(int(row["seq"]), json.loads(row["payload"])) for row in rows]
