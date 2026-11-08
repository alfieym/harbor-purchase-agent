"""Run lifecycle (S14): persist → broadcast, detach ≠ cancel, replay rebuilds."""

import asyncio
import threading
from contextlib import aclosing
from pathlib import Path

import pytest
from pydantic import BaseModel

from harbor.features.runs.service import RunManager
from harbor.features.runs.store import SqliteRunStore


class FakeResponse(BaseModel):
    final_text: str
    cards: list[dict] = []
    pending_draft: dict | None = None


class Boom(Exception):
    pass


def _manager(tmp_path: Path, runner) -> tuple[RunManager, SqliteRunStore]:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    return RunManager(store, runner, heartbeat_seconds=0.05), store


async def _success(buyer_id, session_id, query, on_tool_results, on_text_delta):
    if on_tool_results is not None:
        await on_tool_results(
            [
                {
                    "id": "call-1",
                    "name": "product_search",
                    "arguments": '{"query":"台灯"}',
                    "output": "found 3",
                }
            ]
        )
    return FakeResponse(
        final_text="找到 3 件商品",
        cards=[{"sku_id": "S1", "title": "台灯", "price_major": 99.0, "currency": "CNY"}],
    )


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


class _BlockingFinishStore(SqliteRunStore):
    """Parks the first ``finished`` status write until the test releases it.

    That pause is the window the bug lives in: the terminal event is already
    durable, ``_execute`` is still finishing its bookkeeping, and a
    cancellation arrives (a client cancel, or ``asyncio.run`` tearing down).
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def finish_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        if status == "finished":
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("the test never released finish_run")
        super().finish_run(run_id, status=status, error=error)


def test_start_persists_run_started_before_returning(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        events = store.list_events(handle.run_id)
        await manager.shutdown()
        return handle, events

    handle, events = asyncio.run(scenario())

    assert handle.thread_id == "s1"
    assert events[0] == (1, {"type": "RUN_STARTED", "threadId": "s1", "runId": handle.run_id})


def test_every_broadcast_event_is_already_durable(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        seen = []
        async for item in manager.events(handle.run_id):
            if item is None:
                continue
            seq, event = item
            # The rule under test: by the time a subscriber sees seq N, the same
            # event is already readable from SQLite at seq N.
            assert store.list_events(handle.run_id, after_seq=seq - 1)[0] == (seq, event)
            seen.append(seq)
            if event["type"] == "RUN_FINISHED":
                break
        return seen

    seqs = asyncio.run(scenario())
    assert seqs == list(range(1, len(seqs) + 1))


def test_the_status_is_terminal_before_the_ending_is_broadcast(tmp_path: Path) -> None:
    """Seeing the ending and reading the run's status must not disagree.

    ``GET /agui/runs/{id}`` right after the terminal frame used to answer
    ``running``: the event was durable, but the status was written after the
    broadcast. One transaction removes the window.
    """

    async def scenario():
        manager, store = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        async for item in manager.events(handle.run_id):
            if item is None:
                continue
            _, event = item
            if event["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                return store.get_run(handle.run_id)
        raise AssertionError("the run never reached a terminal event")

    record = asyncio.run(scenario())
    assert record is not None
    assert record.status == "finished"


def test_a_successful_run_emits_the_agui_sequence(tmp_path: Path) -> None:
    async def scenario():
        manager, _ = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        return handle, await _collect(manager, handle.run_id)

    handle, events = asyncio.run(scenario())
    types = [event["type"] for _, event in events]

    assert types == [
        "RUN_STARTED",
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STATE_SNAPSHOT",
        "STEP_FINISHED",
        "RUN_FINISHED",
    ]
    tool_start = events[3][1]
    assert tool_start["toolCallName"] == "product_search"
    assert tool_start["parentMessageId"] == events[2][1]["messageId"]
    snapshot = events[9][1]["snapshot"]
    assert snapshot["cards"][0]["sku_id"] == "S1"
    assert events[-1][1] == {
        "type": "RUN_FINISHED",
        "threadId": "s1",
        "runId": handle.run_id,
        "result": {"status": "finished"},
    }


def test_the_agents_narration_reaches_the_stream_as_it_arrives(tmp_path: Path) -> None:
    """S14-1: prose is streamed, not held until the turn returns."""

    async def narrating(buyer_id, session_id, query, on_tool_results, on_text_delta):
        for chunk in ("我先", "搜一下"):
            await on_text_delta(chunk)
        return FakeResponse(final_text="找到 3 件商品")

    async def scenario():
        manager, _ = _manager(tmp_path, narrating)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        return await _collect(manager, handle.run_id)

    events = asyncio.run(scenario())
    contents = [event["delta"] for _, event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    # The narration arrives in pieces and the guarded final answer still
    # lands once, in the same message, at the end.
    assert contents == ["我先", "搜一下", "找到 3 件商品"]


def test_disconnecting_a_subscriber_does_not_cancel_the_run(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        stream = manager.events(handle.run_id)
        first = await stream.__anext__()
        await stream.aclose()  # the browser tab goes away
        assert first[1]["type"] == "RUN_STARTED"

        for _ in range(100):
            if store.get_run(handle.run_id).status != "running":
                break
            await asyncio.sleep(0.01)
        return handle, store

    handle, store = asyncio.run(scenario())
    assert store.get_run(handle.run_id).status == "finished"
    assert [event["type"] for _, event in store.list_events(handle.run_id)][-1] == "RUN_FINISHED"


def test_replay_from_zero_rebuilds_a_finished_run(tmp_path: Path) -> None:
    async def scenario():
        manager, _ = _manager(tmp_path, _success)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        await _collect(manager, handle.run_id)
        return handle, await _collect(manager, handle.run_id)

    handle, replayed = asyncio.run(scenario())

    assert [event["type"] for _, event in replayed][0] == "RUN_STARTED"
    assert [event["type"] for _, event in replayed][-1] == "RUN_FINISHED"
    assert [seq for seq, _ in replayed] == list(range(1, len(replayed) + 1))


def test_cancel_is_an_explicit_command(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def slow(buyer_id, session_id, query, on_tool_results, on_text_delta):
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("the cancelled turn must not finish")

    async def scenario():
        manager, store = _manager(tmp_path, slow)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="慢慢找")
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.cancel(handle.run_id)
        events = await _collect(manager, handle.run_id)
        await asyncio.sleep(0.05)
        return handle, store, events

    handle, store, events = asyncio.run(scenario())

    assert events[-1][1]["type"] == "RUN_FINISHED"
    assert events[-1][1]["result"] == {"status": "cancelled"}
    assert store.get_run(handle.run_id).status == "cancelled"


def test_cancel_after_the_terminal_event_cannot_add_a_second_ending(tmp_path: Path) -> None:
    """RUN_FINISHED is the end of the story; a later cancel changes nothing.

    The interesting window is between the durable ``RUN_FINISHED`` and the
    ``runs.status`` write. Cancelling there used to append a ``cancelled``
    snapshot and a second ``RUN_FINISHED``, so a replay saw two endings and a
    different final status than the stream.
    """
    store = _BlockingFinishStore(tmp_path / "runs.sqlite3")
    manager = RunManager(store, _success, heartbeat_seconds=0.05)

    async def scenario():
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        async with aclosing(manager.events(handle.run_id)) as stream:
            async for item in stream:
                if item is not None and item[1]["type"] == "RUN_FINISHED":
                    break
        for _ in range(400):
            if store.entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert store.entered.is_set()
        await manager.cancel(handle.run_id)
        store.release.set()
        for _ in range(200):
            if store.get_run(handle.run_id).status != "running":
                break
            await asyncio.sleep(0.01)
        return handle

    handle = asyncio.run(scenario())

    events = store.list_events(handle.run_id)
    endings = [event for _, event in events if event["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert len(endings) == 1
    assert endings[0]["result"] == {"status": "finished"}
    assert store.get_run(handle.run_id).status == "finished"


def test_a_failed_turn_emits_a_stable_error_code(tmp_path: Path) -> None:
    async def boom(buyer_id, session_id, query, on_tool_results, on_text_delta):
        raise Boom("internal detail that must not reach the buyer")

    async def scenario():
        manager, _ = _manager(tmp_path, boom)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        return await _collect(manager, handle.run_id)

    events = asyncio.run(scenario())
    error = events[-1][1]

    assert error["type"] == "RUN_ERROR"
    assert error["code"] == "TURN_FAILED"
    assert "internal detail" not in error["message"]


def test_startup_interrupts_runs_orphaned_by_a_restart(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    store.create_run(
        run_id="r1",
        thread_id="t1",
        buyer_id="b1",
        shopping_session_id="s1",
        input_text="找台灯",
        status="running",
    )
    manager = RunManager(store, _success)

    asyncio.run(manager.startup())

    run = store.get_run("r1")
    assert run.status == "interrupted"
    events = store.list_events("r1")
    assert events[-1][1] == {
        "type": "RUN_ERROR",
        "message": "server_restarted",
        "code": "RUN_INTERRUPTED",
    }


def test_a_repeated_run_id_is_idempotent(tmp_path: Path) -> None:
    async def scenario():
        manager, store = _manager(tmp_path, _success)
        first = await manager.start_run(
            buyer_id="b1", session_id="s1", text="找台灯", run_id="fixed-run"
        )
        second = await manager.start_run(
            buyer_id="b1", session_id="s1", text="找台灯", run_id="fixed-run"
        )
        await _collect(manager, "fixed-run")
        return first, second, store

    first, second, store = asyncio.run(scenario())

    assert first.created is True
    assert second.created is False
    assert [event["type"] for _, event in store.list_events("fixed-run")].count("RUN_STARTED") == 1


def test_unknown_run_is_reported(tmp_path: Path) -> None:
    from harbor.features.runs.store import RunNotFound

    async def scenario():
        manager, _ = _manager(tmp_path, _success)
        with pytest.raises(RunNotFound):
            await _collect(manager, "missing")

    asyncio.run(scenario())


def test_shutdown_marks_in_flight_runs_interrupted(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def slow(buyer_id, session_id, query, on_tool_results, on_text_delta):
        started.set()
        await asyncio.sleep(10)

    async def scenario():
        manager, store = _manager(tmp_path, slow)
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="慢慢找")
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.shutdown()
        return handle, store

    handle, store = asyncio.run(scenario())

    assert store.get_run(handle.run_id).status == "interrupted"
    assert store.list_events(handle.run_id)[-1][1]["code"] == "RUN_INTERRUPTED"
