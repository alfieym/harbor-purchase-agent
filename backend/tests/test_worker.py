"""The S15 worker: a queue message wakes it, the journal decides what to do.

Every test here drives the worker through the journal the API would have
written (a queued row), never through in-process state, because that is the
only thing two processes share.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from harbor.features.runs.queue import MemoryRunQueue
from harbor.features.runs.store import SqliteRunStore
from harbor.features.runs.worker import RunWorker


class FakeResponse(BaseModel):
    final_text: str
    cards: list[dict] = []
    pending_draft: dict | None = None


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingRunner:
    """Counts executions and optionally runs a hook mid-turn."""

    def __init__(self, hook=None) -> None:
        self.calls = 0
        self._hook = hook

    async def __call__(self, buyer_id, session_id, text, on_tool_results, on_text_delta):
        self.calls += 1
        if self._hook is not None:
            await self._hook(self.calls)
        if on_text_delta is not None:
            await on_text_delta("我先看看")
        return FakeResponse(final_text="找到 3 件商品")


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path: Path, clock=None) -> SqliteRunStore:
    return SqliteRunStore(tmp_path / "runs.sqlite3", clock=clock)


def _queued(store: SqliteRunStore, run_id: str = "r1", *, session: str = "s1") -> None:
    store.create_run(
        run_id=run_id,
        thread_id=f"t-{run_id}",
        buyer_id="b1",
        shopping_session_id=session,
        input_text="找台灯",
    )


def _worker(store, runner, queue=None, **kwargs) -> RunWorker:
    options = {"consumer": "worker-a", "claim_idle_ms": 60_000}
    options.update(kwargs)
    return RunWorker(store, queue or MemoryRunQueue(), runner, **options)


def _events(store: SqliteRunStore, run_id: str = "r1") -> list[dict]:
    return [event for _, event in store.list_events(run_id)]


# ------------------------------------------------------------- claim and run


def test_a_queued_run_is_executed_and_acknowledged(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        await queue.enqueue("r1")
        claimed = await queue.claim("worker-a", block_ms=10)

        status = await worker.handle(claimed)
        # Nothing is left for another consumer.
        left = await queue.claim("worker-b", block_ms=10)
        return status, left, store, runner

    status, left, store, runner = _run(scenario())

    assert status == "finished"
    assert left is None
    assert runner.calls == 1
    assert store.get_run("r1").status == "finished"
    assert [event["type"] for event in _events(store)][-1] == "RUN_FINISHED"


def test_the_execution_leaves_a_replayable_journal(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        queue = MemoryRunQueue()
        worker = _worker(store, RecordingRunner(), queue)
        _queued(store)
        await queue.enqueue("r1")
        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return store

    store = _run(scenario())
    types = [event["type"] for event in _events(store)]

    # The API already wrote RUN_STARTED when it registered the run; the worker
    # continues the same message and ends it exactly once.
    assert types == [
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STATE_SNAPSHOT",
        "STEP_FINISHED",
        "RUN_FINISHED",
    ]
    assert _events(store)[-1]["result"] == {"status": "finished"}


def test_a_duplicate_message_does_not_execute_twice(tmp_path: Path) -> None:
    """At-least-once delivery is safe because the claim is a CAS on the row."""

    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        await queue.enqueue("r1")
        await queue.enqueue("r1")
        first = await queue.claim("worker-a", block_ms=10)
        second = await queue.claim("worker-a", block_ms=10)

        return (
            await worker.handle(first),
            await worker.handle(second),
            runner.calls,
            store,
        )

    first, second, calls, store = _run(scenario())

    assert (first, second) == ("finished", None)
    assert calls == 1
    assert [e["type"] for e in _events(store)].count("RUN_FINISHED") == 1


def test_a_started_run_is_never_reexecuted(tmp_path: Path) -> None:
    """Its narration is already on someone's screen; the sweeper owns the end."""

    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        assert store.try_claim("r1", "the-dead-worker") is True
        await queue.enqueue("r1")

        status = await worker.handle(await queue.claim("worker-a", block_ms=10))
        return status, runner.calls, store

    status, calls, store = _run(scenario())

    assert status is None
    assert calls == 0
    assert store.get_run("r1").status == "running"


def test_an_unknown_run_is_acknowledged(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        await queue.enqueue("ghost")
        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return await queue.claim("worker-b", block_ms=10), runner.calls

    left, calls = _run(scenario())
    assert left is None
    assert calls == 0


def test_a_cancelled_run_is_not_executed(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        store.request_cancel("r1")  # queued -> cancelled, atomically
        await queue.enqueue("r1")

        status = await worker.handle(await queue.claim("worker-a", block_ms=10))
        return status, runner.calls

    assert _run(scenario()) == (None, 0)


def test_a_busy_session_defers_the_run(tmp_path: Path) -> None:
    """One conversation at a time; the sweeper re-sends the queued run later."""

    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        assert store.acquire_session("s1", "someone-else", "worker-z") is True
        await queue.enqueue("r1")

        status = await worker.handle(await queue.claim("worker-a", block_ms=10))
        return status, runner.calls, store

    status, calls, store = _run(scenario())

    assert status is None
    assert calls == 0
    # Still queued: nothing was claimed, so the run is not lost.
    assert store.get_run("r1").status == "queued"


def test_the_session_lease_is_released_when_the_turn_ends(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        queue = MemoryRunQueue()
        worker = _worker(store, RecordingRunner(), queue)
        _queued(store)
        await queue.enqueue("r1")
        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return store.acquire_session("s1", "next-run", "worker-b")

    assert _run(scenario()) is True


def test_a_failing_turn_ends_the_run_and_releases_the_message(tmp_path: Path) -> None:
    class Boom(Exception):
        pass

    async def boom(buyer_id, session_id, text, on_tool_results, on_text_delta):
        raise Boom("internal detail")

    async def scenario():
        store = _store(tmp_path)
        queue = MemoryRunQueue()
        worker = _worker(store, boom, queue)
        _queued(store)
        await queue.enqueue("r1")

        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return store, await queue.claim("worker-b", block_ms=10)

    store, left = _run(scenario())

    assert left is None
    record = store.get_run("r1")
    assert (record.status, record.error) == ("failed", "TURN_FAILED")
    assert _events(store)[-1]["code"] == "TURN_FAILED"


def test_a_cancel_request_stops_the_turn_and_ends_it_as_cancelled(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)

        async def cancel_mid_turn(_call: int) -> None:
            # The API's cancel arrives while the worker is narrating.
            store.request_cancel("r1")

        runner = RecordingRunner(cancel_mid_turn)
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue)
        _queued(store)
        await queue.enqueue("r1")

        status = await worker.handle(await queue.claim("worker-a", block_ms=10))
        return status, store

    status, store = _run(scenario())

    assert status == "cancelled"
    assert store.get_run("r1").status == "cancelled"
    assert _events(store)[-1]["result"] == {"status": "cancelled"}


# ------------------------------------------------------------------- sweeper


def test_the_sweeper_interrupts_a_run_whose_worker_disappeared(tmp_path: Path) -> None:
    async def scenario():
        clock = FakeClock()
        store = _store(tmp_path, clock)
        worker = _worker(store, RecordingRunner(), lease_seconds=90)
        _queued(store)
        assert store.try_claim("r1", "dead-worker") is True
        clock.advance(91)

        await worker.sweep()
        return store

    store = _run(scenario())

    record = store.get_run("r1")
    assert (record.status, record.error) == ("interrupted", "worker_lost")
    assert _events(store)[-1] == {
        "type": "RUN_ERROR",
        "message": "worker_lost",
        "code": "RUN_INTERRUPTED",
    }


def test_the_sweeper_leaves_a_live_worker_alone(tmp_path: Path) -> None:
    async def scenario():
        clock = FakeClock()
        store = _store(tmp_path, clock)
        worker = _worker(store, RecordingRunner(), lease_seconds=90)
        _queued(store)
        store.try_claim("r1", "busy-worker")
        clock.advance(30)
        assert store.heartbeat("r1", "busy-worker") is True
        clock.advance(30)

        await worker.sweep()
        return store

    assert _run(scenario()).get_run("r1").status == "running"


def test_the_sweeper_requeues_a_run_nobody_picked_up(tmp_path: Path) -> None:
    async def scenario():
        clock = FakeClock()
        store = _store(tmp_path, clock)
        queue = MemoryRunQueue()
        worker = _worker(store, RecordingRunner(), queue, requeue_after_seconds=15)
        _queued(store)
        clock.advance(16)

        await worker.sweep()
        first = await queue.claim("worker-a", block_ms=10)
        # A second sweep inside the grace window must not enqueue it again.
        await worker.sweep()
        second = await queue.claim("worker-b", block_ms=10)
        return first, second

    first, second = _run(scenario())

    assert first is not None
    assert first.run_id == "r1"
    assert second is None


def test_the_sweeper_purges_expired_session_leases(tmp_path: Path) -> None:
    async def scenario():
        clock = FakeClock()
        store = _store(tmp_path, clock)
        worker = _worker(store, RecordingRunner())
        assert store.acquire_session("s1", "r0", "dead-worker", ttl_seconds=60) is True
        clock.advance(61)

        await worker.sweep()
        return store.acquire_session("s1", "r1", "worker-a")

    assert _run(scenario()) is True


# -------------------------------------------------------------- the real loop


def test_the_loop_drains_the_queue(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        runner = RecordingRunner()
        queue = MemoryRunQueue()
        worker = _worker(store, runner, queue, sweep_seconds=999)
        for index in range(3):
            _queued(store, f"r{index}", session=f"s{index}")
            await queue.enqueue(f"r{index}")

        task = asyncio.create_task(worker.run())
        for _ in range(200):
            if all(store.get_run(f"r{i}").status == "finished" for i in range(3)):
                break
            await asyncio.sleep(0.01)
        await worker.stop()
        await task
        return store, runner

    store, runner = _run(scenario())

    assert runner.calls == 3
    assert [store.get_run(f"r{i}").status for i in range(3)] == ["finished"] * 3


def test_stopping_the_worker_interrupts_the_run_it_was_executing(tmp_path: Path) -> None:
    """A rolling deploy must not leave a row ``running`` for a process that is gone."""

    started = asyncio.Event()

    async def stalled(buyer_id, session_id, text, on_tool_results, on_text_delta):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("a cancelled turn must not finish")

    async def scenario():
        store = _store(tmp_path)
        queue = MemoryRunQueue()
        worker = _worker(store, stalled, queue, sweep_seconds=999)
        _queued(store)
        await queue.enqueue("r1")

        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        await worker.stop()
        await task
        return store

    store = _run(scenario())

    record = store.get_run("r1")
    assert (record.status, record.error) == ("interrupted", "server_restarted")
    assert _events(store)[-1]["code"] == "RUN_INTERRUPTED"


def test_a_worker_refuses_to_start_without_a_model() -> None:
    """Better a failed container than one that claims runs it cannot answer."""

    from harbor.core.config import Settings
    from harbor.worker import build_worker

    settings = Settings(_env_file=None, openai_api_key="", queue_enabled=True)
    with pytest.raises(RuntimeError, match="configured model"):
        build_worker(settings)


def test_the_worker_owner_is_recorded_so_heartbeats_can_be_checked(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path)
        queue = MemoryRunQueue()
        worker = _worker(store, RecordingRunner(), queue)
        _queued(store)
        await queue.enqueue("r1")
        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return store.get_run("r1").owner

    assert _run(scenario()) == "worker-a"
