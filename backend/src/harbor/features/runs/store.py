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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
"""


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRunStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

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
        status: str = "running",
    ) -> None:
        now = _now()
        with self._lock, self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, thread_id, buyer_id, shopping_session_id,"
                    " input_text, status, error, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        run_id,
                        thread_id,
                        buyer_id,
                        shopping_session_id,
                        input_text,
                        status,
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
                now = _now()
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
                (status, error, _now(), run_id),
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
                    [("server_restarted", _now(), run_id) for run_id in ids],
                )
        return ids

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
        )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[tuple[int, dict]]:
        with self._connection() as conn:
            self._require(conn, run_id)
            rows = conn.execute(
                "SELECT seq, payload FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [(int(row["seq"]), json.loads(row["payload"])) for row in rows]
