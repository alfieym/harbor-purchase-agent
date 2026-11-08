"""Run journal, S15: queued rows, one owner per run, leases that expire.

The store is where S15's concurrency lives. Every guarantee below is a single
SQL statement whose ``rowcount`` decides the winner — not a check-then-write
pair that two processes could interleave.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harbor.features.runs.store import SqliteRunStore


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _store(tmp_path: Path) -> tuple[SqliteRunStore, FakeClock]:
    clock = FakeClock()
    return SqliteRunStore(tmp_path / "runs.sqlite3", clock=clock), clock


def _create(store: SqliteRunStore, run_id: str = "r1") -> None:
    store.create_run(
        run_id=run_id,
        thread_id="t1",
        buyer_id="b1",
        shopping_session_id="s1",
        input_text="找台灯",
    )


# ----------------------------------------------------------------- queued rows


def test_a_new_run_waits_in_the_queue(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)

    run = store.get_run("r1")
    assert run.status == "queued"
    assert run.owner is None


def test_only_one_worker_can_claim_a_run(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)

    assert store.try_claim("r1", "worker-a") is True
    # The second claim is the duplicate delivery being absorbed: same message,
    # two consumers, one execution.
    assert store.try_claim("r1", "worker-b") is False

    run = store.get_run("r1")
    assert run.status == "running"
    assert run.owner == "worker-a"


def test_claiming_a_finished_run_is_refused(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)
    store.finish_run("r1", status="finished")

    assert store.try_claim("r1", "worker-a") is False
    assert store.get_run("r1").status == "finished"


# ----------------------------------------------------------------- heartbeats


def test_only_the_owner_can_refresh_a_heartbeat(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)
    store.try_claim("r1", "worker-a")

    assert store.heartbeat("r1", "worker-a") is True
    assert store.heartbeat("r1", "worker-b") is False


def test_a_run_whose_heartbeat_stopped_is_interrupted(tmp_path: Path) -> None:
    """A dead worker must not leave a run ``running`` forever."""
    store, clock = _store(tmp_path)
    _create(store)
    store.try_claim("r1", "worker-a")
    clock.advance(120)

    interrupted = store.interrupt_stale(lease_seconds=90)

    assert interrupted == ["r1"]
    run = store.get_run("r1")
    assert run.status == "interrupted"
    assert run.error == "worker_lost"


def test_a_live_heartbeat_protects_a_run_from_the_sweeper(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    _create(store)
    store.try_claim("r1", "worker-a")
    clock.advance(60)
    store.heartbeat("r1", "worker-a")
    clock.advance(60)

    assert store.interrupt_stale(lease_seconds=90) == []
    assert store.get_run("r1").status == "running"


# ------------------------------------------------------------- requeue sweep


def test_queued_runs_are_requeued_only_after_the_grace_period(tmp_path: Path) -> None:
    """A message lost between commit and XADD is recovered from the journal."""
    store, clock = _store(tmp_path)
    _create(store, "r1")

    assert store.stale_queued(seconds=15) == []

    clock.advance(20)
    assert store.stale_queued(seconds=15) == ["r1"]

    # A successful enqueue pushes the deadline out again.
    store.mark_enqueued("r1")
    assert store.stale_queued(seconds=15) == []


def test_a_claimed_run_is_not_requeued(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    _create(store)
    store.try_claim("r1", "worker-a")
    clock.advance(600)

    assert store.stale_queued(seconds=15) == []


# ------------------------------------------------------------------- cancel


def test_cancelling_a_queued_run_is_a_cas(tmp_path: Path) -> None:
    """Whoever wins ``queued`` decides the ending; the loser does nothing."""
    store, _ = _store(tmp_path)
    _create(store)

    assert store.request_cancel("r1") is True
    assert store.get_run("r1").status == "cancelled"
    assert store.request_cancel("r1") is False
    assert store.try_claim("r1", "worker-a") is False


def test_cancelling_a_running_run_leaves_a_flag_for_its_worker(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)
    store.try_claim("r1", "worker-a")

    assert store.request_cancel("r1") is False
    assert store.cancel_requested("r1") is True
    assert store.get_run("r1").status == "running"


def test_a_cancelled_run_cannot_be_claimed(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store)
    store.request_cancel("r1")

    assert store.try_claim("r1", "worker-a") is False


# ------------------------------------------------------------ session leases


def test_a_session_admits_one_run_at_a_time(tmp_path: Path) -> None:
    """Two processes share no ``asyncio.Lock``; the lease is what remains."""
    store, _ = _store(tmp_path)
    _create(store, "r1")
    _create(store, "r2")

    assert store.acquire_session("s1", "r1", "worker-a", ttl_seconds=60) is True
    assert store.acquire_session("s1", "r2", "worker-b", ttl_seconds=60) is False
    # A different session is a different conversation, and moves freely.
    assert store.acquire_session("s2", "r2", "worker-b", ttl_seconds=60) is True

    store.release_session("s1", "r1")
    assert store.acquire_session("s1", "r2", "worker-b", ttl_seconds=60) is True


def test_only_the_holder_can_release_a_session_lease(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    _create(store, "r1")
    _create(store, "r2")
    store.acquire_session("s1", "r1", "worker-a", ttl_seconds=60)

    store.release_session("s1", "r2")

    assert store.acquire_session("s1", "r2", "worker-b", ttl_seconds=60) is False


def test_an_expired_lease_is_taken_over(tmp_path: Path) -> None:
    """The holder may have died; a lease nobody renews must not lock forever."""
    store, clock = _store(tmp_path)
    _create(store, "r1")
    _create(store, "r2")
    store.acquire_session("s1", "r1", "worker-a", ttl_seconds=60)
    clock.advance(90)

    assert store.acquire_session("s1", "r2", "worker-b", ttl_seconds=60) is True
    # ...and the original holder's late release must not free someone else's.
    store.release_session("s1", "r1")
    assert store.acquire_session("s1", "r3", "worker-c", ttl_seconds=60) is False


# ----------------------------------------------------------------- migration


def test_an_s14_database_gains_the_s15_columns(tmp_path: Path) -> None:
    """Existing deployments have a runs table without these columns."""
    path = tmp_path / "runs.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
            shopping_session_id TEXT NOT NULL, input_text TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE run_events (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (run_id, seq)
        );
        INSERT INTO runs VALUES ('old','t','b','s','hi','running',NULL,
                                 '2026-11-01T00:00:00+00:00','2026-11-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = SqliteRunStore(path)

    run = store.get_run("old")
    assert run is not None
    assert run.status == "running"
    assert run.owner is None
    # The dormant row is never adopted: an unclaimed ``running`` row is the
    # sweeper's business, not a claim's.
    assert store.try_claim("old", "worker-a") is False
    # ...but the new machinery works on the migrated file.
    _create(store, "new")
    assert store.try_claim("new", "worker-a") is True
