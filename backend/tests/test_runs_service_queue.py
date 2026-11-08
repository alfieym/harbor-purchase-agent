"""S15 API mode: register the run, enqueue it, follow the journal.

The API never executes a turn here. These tests hold it to exactly three
powers — write the row, send the nudge, read the log — and to the rule that
losing the nudge is survivable while losing the row is not.
"""

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest
from pydantic import BaseModel

from harbor.features.runs import agui
from harbor.features.runs.queue import MemoryRunQueue
from harbor.features.runs.service import RunManager
from harbor.features.runs.store import RunNotFound, SqliteRunStore
from harbor.features.runs.worker import RunWorker


class FakeResponse(BaseModel):
    final_text: str
    cards: list[dict] = []
    pending_draft: dict | None = None


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, buyer_id, session_id, text, on_tool_results, on_text_delta):
        self.calls += 1
        return FakeResponse(final_text="找到 3 件商品")


class BrokenQueue:
    """Redis is down. Nothing else may notice."""

    async def enqueue(self, run_id: str) -> None:
        raise ConnectionError("redis is down")

    async def claim(self, consumer: str, *, block_ms: int = 5000):
        return None

    async def ack(self, message_id: str) -> None:
        return None

    async def reclaim(self, consumer: str, *, min_idle_ms: int, count: int = 10):
        return []

    async def close(self) -> None:
        return None


def _run(coro):
    return asyncio.run(coro)


def _manager(tmp_path: Path, runner=None, queue=None) -> tuple[RunManager, SqliteRunStore]:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    manager = RunManager(
        store,
        runner or CountingRunner(),
        queue=queue if queue is not None else MemoryRunQueue(),
        follow_poll_seconds=0.01,
        heartbeat_seconds=0.05,
    )
    return manager, store


async def _collect(manager: RunManager, run_id: str, *, after: int = 0) -> list[tuple[int, dict]]:
    events: list[tuple[int, dict]] = []
    async with aclosing(manager.events(run_id, after_seq=after)) as stream:
        async for item in stream:
            if item is None:
                continue
            seq, event = item
            events.append((seq, event))
            if event["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                break
    return events


# ------------------------------------------------------------------ register


def test_start_run_registers_and_enqueues_but_never_executes(tmp_path: Path) -> None:
    async def scenario():
        runner = CountingRunner()
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, runner, queue)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        claimed = await queue.claim("worker-a", block_ms=10)
        return handle, claimed, runner.calls, store

    handle, claimed, calls, store = _run(scenario())

    assert calls == 0  # the API did not run the turn
    assert claimed is not None and claimed.run_id == handle.run_id
    record = store.get_run(handle.run_id)
    assert record is not None and record.status == "queued"
    assert [e["type"] for _, e in store.list_events(handle.run_id)] == ["RUN_STARTED"]


def test_a_redis_outage_loses_the_nudge_not_the_run(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path, queue=BrokenQueue())
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        return handle, store

    handle, store = _run(scenario())

    # The row and its first event are committed before the enqueue is tried,
    # so the sweeper can find and re-send this run later.
    assert store.get_run(handle.run_id).status == "queued"
    assert [e["type"] for _, e in store.list_events(handle.run_id)] == ["RUN_STARTED"]


def test_a_repeated_run_id_is_still_idempotent(tmp_path: Path) -> None:
    async def scenario():
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, queue=queue)
        first = await manager.start_run(
            buyer_id="b1", session_id="s1", text="找台灯", run_id="fixed-run"
        )
        second = await manager.start_run(
            buyer_id="b1", session_id="s1", text="找台灯", run_id="fixed-run"
        )
        return first, second, store

    first, second, store = _run(scenario())

    assert (first.created, second.created) == (True, False)
    assert [e["type"] for _, e in store.list_events("fixed-run")].count("RUN_STARTED") == 1


# -------------------------------------------------------------------- follow


def test_the_api_streams_what_the_worker_writes(tmp_path: Path) -> None:
    """The reader is a journal tail; nothing here is delivered in memory."""

    async def scenario():
        runner = CountingRunner()
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, runner, queue)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        claimed = await queue.claim("worker-a", block_ms=10)
        worker = RunWorker(store, queue, runner, consumer="worker-a", sweep_seconds=999)

        async def drive():
            await asyncio.sleep(0.03)
            await worker.handle(claimed)

        task = asyncio.create_task(drive())
        events = await _collect(manager, handle.run_id)
        await task
        return events

    events = _run(scenario())
    types = [event["type"] for _, event in events]

    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "TEXT_MESSAGE_CONTENT" in types
    assert [seq for seq, _ in events] == list(range(1, len(events) + 1))


def test_a_run_that_ended_while_nobody_was_watching_replays_from_the_journal(
    tmp_path: Path,
) -> None:
    async def scenario():
        runner = CountingRunner()
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, runner, queue)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        worker = RunWorker(store, queue, runner, consumer="worker-a", sweep_seconds=999)
        await worker.handle(await queue.claim("worker-a", block_ms=10))

        # A refresh after the fact: the API process may be a different one.
        return await _collect(manager, handle.run_id)

    events = _run(scenario())

    assert events[0][1]["type"] == "RUN_STARTED"
    assert events[-1][1] == {
        "type": "RUN_FINISHED",
        "threadId": "s1",
        "runId": events[-1][1]["runId"],
        "result": {"status": "finished"},
    }


def test_two_readers_follow_the_same_run_independently(tmp_path: Path) -> None:
    """No subscriber set to miss an event: both readers tail the same log."""

    async def scenario():
        runner = CountingRunner()
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, runner, queue)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        claimed = await queue.claim("worker-a", block_ms=10)
        worker = RunWorker(store, queue, runner, consumer="worker-a", sweep_seconds=999)

        async def drive():
            await asyncio.sleep(0.03)
            await worker.handle(claimed)

        driver = asyncio.create_task(drive())
        first, second = await asyncio.gather(
            _collect(manager, handle.run_id), _collect(manager, handle.run_id)
        )
        await driver
        return first, second

    first, second = _run(scenario())
    assert [e["type"] for _, e in first] == [e["type"] for _, e in second]
    assert first == second


def test_a_reader_does_not_miss_events_written_while_it_checks_the_status(
    tmp_path: Path,
) -> None:
    """The follower's two reads are not one snapshot.

    It polls the log, finds nothing, then reads the row to see whether the run
    is over. A worker may commit the rest of the journal between those two
    reads: the row already says ``finished`` while events this reader has not
    seen exist. Returning there drops the ending, so the follower must drain
    the log once more before closing.
    """
    run_id = "run_racing"
    path = tmp_path / "runs.sqlite3"

    def write_prefix(store: SqliteRunStore) -> None:
        store.create_run(
            run_id=run_id,
            thread_id="t1",
            buyer_id="b1",
            shopping_session_id="s1",
            input_text="找台灯",
            status="queued",
        )
        store.append_event(run_id, agui.run_started(thread_id="t1", run_id=run_id))
        store.append_event(run_id, agui.step_started("agent"))
        store.append_event(run_id, agui.text_message_start("m1"))

    def write_ending(store: SqliteRunStore) -> None:
        store.append_event(run_id, agui.text_message_content("m1", "找到 3 件商品"))
        store.append_event(run_id, agui.text_message_end("m1"))
        store.append_event(run_id, agui.state_snapshot({}))
        store.append_event(run_id, agui.step_finished("agent"))
        store.append_event(
            run_id,
            agui.run_finished(thread_id="t1", run_id=run_id, result={"status": "finished"}),
            status="finished",
        )

    class WorkerFinishesMidRead(SqliteRunStore):
        """Fires the ending exactly when the follower asks for the row."""

        def __init__(self) -> None:
            super().__init__(path)
            self._get_runs = 0

        def get_run(self, run_id: str):
            self._get_runs += 1
            # Call 1 is ``events``'s existence check; call 2 is the follower
            # asking whether the run is over after an empty poll.
            if self._get_runs == 2:
                write_ending(self)
            return super().get_run(run_id)

    async def scenario():
        store = WorkerFinishesMidRead()
        write_prefix(store)
        manager = RunManager(
            store,
            CountingRunner(),
            queue=MemoryRunQueue(),
            follow_poll_seconds=0.01,
            heartbeat_seconds=0.05,
        )
        return await _collect(manager, run_id)

    events = _run(scenario())
    types = [event["type"] for _, event in events]

    assert types == [
        "RUN_STARTED",
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STATE_SNAPSHOT",
        "STEP_FINISHED",
        "RUN_FINISHED",
    ]


def test_following_an_unknown_run_reports_it(tmp_path: Path) -> None:
    async def scenario():
        manager, _ = _manager(tmp_path)
        with pytest.raises(RunNotFound):
            await _collect(manager, "missing")

    _run(scenario())


# -------------------------------------------------------------------- cancel


def test_cancelling_a_queued_run_ends_it_without_a_worker(tmp_path: Path) -> None:
    """Nobody holds the event log's write order but us, so we may close it."""

    async def scenario():
        manager, store = _manager(tmp_path)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        record = await manager.cancel(handle.run_id)
        return record, store

    record, store = _run(scenario())

    assert record.status == "cancelled"
    events = [e for _, e in store.list_events(record.run_id)]
    assert [e["type"] for e in events] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    assert events[-1]["result"] == {"status": "cancelled"}


def test_cancelling_a_running_run_only_asks_its_worker(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        # A worker has claimed it and is executing; the terminal events must be
        # written by the process that owns the stream.
        assert store.try_claim(handle.run_id, "worker-a") is True
        record = await manager.cancel(handle.run_id)
        return record, store

    record, store = _run(scenario())

    assert record.status == "running"
    assert store.cancel_requested(record.run_id) is True
    assert [e["type"] for _, e in store.list_events(record.run_id)] == ["RUN_STARTED"]


def test_cancelling_a_finished_run_changes_nothing(tmp_path: Path) -> None:
    async def scenario():
        runner = CountingRunner()
        queue = MemoryRunQueue()
        manager, store = _manager(tmp_path, runner, queue)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        worker = RunWorker(store, queue, runner, consumer="worker-a", sweep_seconds=999)
        await worker.handle(await queue.claim("worker-a", block_ms=10))
        before = len(store.list_events(handle.run_id))

        record = await manager.cancel(handle.run_id)
        return record, len(store.list_events(handle.run_id)), before

    record, after, before = _run(scenario())

    assert record.status == "finished"
    assert after == before


def test_cancelling_an_unknown_run_reports_it(tmp_path: Path) -> None:
    async def scenario():
        manager, _ = _manager(tmp_path)
        with pytest.raises(RunNotFound):
            await manager.cancel("missing")

    _run(scenario())


# ------------------------------------------------------------------ lifecycle


def test_startup_leaves_running_runs_to_the_sweeper(tmp_path: Path) -> None:
    """Another worker may own that row; the API must not confiscate it."""

    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    store.create_run(
        run_id="r1",
        thread_id="t1",
        buyer_id="b1",
        shopping_session_id="s1",
        input_text="找台灯",
        status="running",
    )
    manager = RunManager(store, CountingRunner(), queue=MemoryRunQueue())

    _run(manager.startup())

    assert store.get_run("r1").status == "running"
    assert store.list_events("r1") == []


def test_shutdown_does_not_touch_rows_the_api_does_not_own(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        await manager.shutdown()
        return handle, store

    handle, store = _run(scenario())

    assert store.get_run(handle.run_id).status == "queued"
