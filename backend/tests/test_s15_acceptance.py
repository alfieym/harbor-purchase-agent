"""S15 acceptance: the API and the worker share one file and nothing else.

Each side builds its own store on the same path, the way two processes would.
Everything they agree on has to travel through the SQLite journal or through a
queue message — if a test here passes by accident of shared memory, it would
not survive a deployment.
"""

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from harbor.features.runs.queue import MemoryRunQueue
from harbor.features.runs.service import RunManager
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


async def _turn(buyer_id, session_id, text, on_tool_results, on_text_delta):
    if on_text_delta is not None:
        await on_text_delta("我先搜一下")
    return FakeResponse(final_text="找到 3 件商品")


class _SilentRunner:
    async def __call__(self, buyer_id, session_id, text, on_tool_results, on_text_delta):
        return FakeResponse(final_text="不该被执行")


class FlakyQueue(MemoryRunQueue):
    """Redis down at first, up later — the shape of a real outage."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    async def enqueue(self, run_id: str) -> None:
        if self.down:
            raise ConnectionError("redis is down")
        await super().enqueue(run_id)


def _run(coro):
    return asyncio.run(coro)


async def _stream(manager: RunManager, run_id: str) -> list[dict]:
    events: list[dict] = []
    async with aclosing(manager.events(run_id)) as stream:
        async for item in stream:
            if item is None:
                continue
            events.append(item[1])
            if item[1]["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                break
    return events


def test_the_two_processes_complete_a_run_through_the_journal_alone(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    queue = MemoryRunQueue()

    # Two stores on one file: the only state the processes share.
    api_store = SqliteRunStore(path)
    worker_store = SqliteRunStore(path)
    manager = RunManager(api_store, _turn, queue=queue, follow_poll_seconds=0.01)
    worker = RunWorker(worker_store, queue, _turn, consumer="worker-a", sweep_seconds=999)

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        claimed = await queue.claim("worker-a", block_ms=10)

        async def drive():
            await asyncio.sleep(0.02)
            await worker.handle(claimed)

        driver = asyncio.create_task(drive())
        events = await _stream(manager, handle.run_id)
        await driver
        return handle, events

    handle, events = _run(scenario())
    types = [event["type"] for event in events]

    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert types.count("RUN_FINISHED") == 1
    assert api_store.get_run(handle.run_id).status == "finished"
    assert events[-1]["result"] == {"status": "finished"}


def test_a_lost_wake_up_is_recovered_by_the_sweeper(tmp_path: Path) -> None:
    """Redis was down when the API registered the run. The row survived."""

    clock = FakeClock()
    path = tmp_path / "runs.sqlite3"
    queue = FlakyQueue()
    store = SqliteRunStore(path, clock=clock)
    manager = RunManager(store, _turn, queue=queue, follow_poll_seconds=0.01)
    worker = RunWorker(
        SqliteRunStore(path, clock=clock),
        queue,
        _turn,
        consumer="worker-a",
        requeue_after_seconds=15,
        sweep_seconds=999,
    )

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        # The registration succeeded; only the nudge was lost.
        assert store.get_run(handle.run_id).status == "queued"
        assert await queue.claim("worker-a", block_ms=10) is None

        queue.down = False
        clock.advance(16)
        await worker.sweep()  # re-sends from the journal

        await worker.handle(await queue.claim("worker-a", block_ms=10))
        return handle, await _stream(manager, handle.run_id)

    handle, events = _run(scenario())

    assert store.get_run(handle.run_id).status == "finished"
    assert [event["type"] for event in events][-1] == "RUN_FINISHED"


def test_a_worker_that_died_mid_run_is_reported_interrupted(tmp_path: Path) -> None:
    """Its narration is already on a screen; restarting it would duplicate it."""

    clock = FakeClock()
    path = tmp_path / "runs.sqlite3"
    queue = MemoryRunQueue()
    api_store = SqliteRunStore(path, clock=clock)
    manager = RunManager(api_store, _turn, queue=queue, follow_poll_seconds=0.01)
    survivor = RunWorker(
        SqliteRunStore(path, clock=clock),
        queue,
        _SilentRunner(),
        consumer="worker-b",
        lease_seconds=90,
        sweep_seconds=999,
    )

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        # A worker claimed it, wrote a frame, and then vanished.
        dead_store = SqliteRunStore(path, clock=clock)
        assert dead_store.try_claim(handle.run_id, "worker-dead") is True
        dead_store.append_event(handle.run_id, {"type": "STEP_STARTED", "stepName": "agent"})

        clock.advance(91)
        await survivor.sweep()
        return handle, await _stream(manager, handle.run_id)

    handle, events = _run(scenario())

    record = api_store.get_run(handle.run_id)
    assert (record.status, record.error) == ("interrupted", "worker_lost")
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "RUN_INTERRUPTED"


def test_a_duplicate_delivery_yields_one_execution_one_ending(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    queue = MemoryRunQueue()
    store = SqliteRunStore(path)
    manager = RunManager(store, _turn, queue=queue, follow_poll_seconds=0.01)
    calls = 0

    async def counting_turn(buyer_id, session_id, text, on_tool_results, on_text_delta):
        nonlocal calls
        calls += 1
        return FakeResponse(final_text="找到 3 件商品")

    worker = RunWorker(
        SqliteRunStore(path), queue, counting_turn, consumer="worker-a", sweep_seconds=999
    )

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        await queue.enqueue(handle.run_id)  # the same run, delivered twice
        first = await queue.claim("worker-a", block_ms=10)
        second = await queue.claim("worker-a", block_ms=10)
        await worker.handle(first)
        await worker.handle(second)
        return handle, await _stream(manager, handle.run_id)

    handle, events = _run(scenario())

    assert calls == 1
    assert [event["type"] for event in events].count("RUN_FINISHED") == 1
    assert store.get_run(handle.run_id).status == "finished"


def test_the_api_restarting_mid_run_does_not_touch_the_row(tmp_path: Path) -> None:
    """Under S14 the API called interrupt_running(); two processes make that wrong."""

    path = tmp_path / "runs.sqlite3"
    store = SqliteRunStore(path)
    queue = MemoryRunQueue()
    manager = RunManager(store, _turn, queue=queue)

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        assert store.try_claim(handle.run_id, "worker-a") is True
        await manager.shutdown()
        await manager.startup()
        return handle

    handle = _run(scenario())
    assert store.get_run(handle.run_id).status == "running"
