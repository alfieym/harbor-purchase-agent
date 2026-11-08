"""Run lifecycle (S14): a run outlives the request that started it.

Three invariants drive this module:

* **Persist, then broadcast.** ``_append`` commits the event and obtains its
  sequence number before any subscriber sees it. A client can therefore
  reconnect with ``after=<seq>`` and miss nothing, and a crash cannot create a
  gap a replay would skip.
* **A disconnect is not a cancel.** The turn runs in its own task; closing the
  SSE stream only removes one queue from ``subscribers``. Cancellation happens
  only when someone calls ``cancel`` (or the process shuts down).
* **Replay is the source of truth.** ``events`` always reads the durable log
  first, then subscribes; both happen under the run lock so no event can slip
  between the two.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from harbor.features.runs import agui
from harbor.features.runs.executor import (
    TERMINAL_STATUSES,
    TextObserver,
    ToolObserver,
    ToolResults,
    TurnRunner,
    execute_turn,
    snapshot,
)
from harbor.features.runs.queue import RunQueue
from harbor.features.runs.store import RunExists, RunNotFound, RunRecord, SqliteRunStore

logger = logging.getLogger(__name__)

__all__ = [
    "RunHandle",
    "RunManager",
    "TextObserver",
    "ToolObserver",
    "ToolResults",
    "TurnRunner",
]

_CLOSED = object()


def _offer(queue: asyncio.Queue, item: Any) -> None:
    """Bounded queue: a stalled subscriber loses old events, never the run."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


@dataclass
class RunHandle:
    run_id: str
    thread_id: str
    created: bool = True


@dataclass
class _ActiveRun:
    run_id: str
    thread_id: str
    buyer_id: str
    session_id: str
    text: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    done: bool = False
    # ``terminal`` means a RUN_FINISHED/RUN_ERROR is already in the durable
    # log. From that point the run is over on the wire: a late cancellation
    # (a client cancel, or the event loop tearing down a finished task) must
    # not narrate a second ending.
    terminal: bool = False
    terminal_status: str | None = None
    terminal_error: str | None = None
    cancelling: bool = False
    task: asyncio.Task | None = None


class RunManager:
    def __init__(
        self,
        store: SqliteRunStore,
        turn_runner: TurnRunner,
        *,
        queue: RunQueue | None = None,
        subscriber_buffer: int = 500,
        heartbeat_seconds: float = 15.0,
        follow_poll_seconds: float = 0.1,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._turn_runner = turn_runner
        # ``queue`` selects the deployment shape. With it, this process only
        # registers runs and tails the journal; without it, turns execute here
        # exactly as they did before S15 (single-process development).
        self._queue = queue
        self._buffer = subscriber_buffer
        self._heartbeat = heartbeat_seconds
        self._follow_poll = follow_poll_seconds
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._runs: dict[str, _ActiveRun] = {}
        self._shutting_down = False

    # ------------------------------------------------------------- lifecycle

    async def startup(self) -> None:
        """Close the books on runs a previous process left ``running``."""
        self._shutting_down = False
        if self._queue is not None:
            # Some worker may be executing those rows right now — the API has
            # no way to tell a live one from a dead one. That is the sweeper's
            # job (a heartbeat that stopped), not an API restart's.
            return
        orphans = await asyncio.to_thread(self._store.interrupt_running)
        for run_id in orphans:
            await asyncio.to_thread(
                self._store.append_event,
                run_id,
                agui.run_error("server_restarted", code="RUN_INTERRUPTED"),
            )

    async def start_run(
        self,
        *,
        buyer_id: str,
        session_id: str,
        text: str,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> RunHandle:
        run_id = run_id or f"run_{self._id_factory()}"
        thread_id = thread_id or session_id
        try:
            await asyncio.to_thread(
                self._store.create_run,
                run_id=run_id,
                thread_id=thread_id,
                buyer_id=buyer_id,
                shopping_session_id=session_id,
                input_text=text,
                # Inline mode executes here and now, so the row is live the
                # moment it exists. In queue mode the worker is the only one
                # that may move a run to ``running``.
                status="running" if self._queue is None else "queued",
            )
        except RunExists:
            record = await asyncio.to_thread(self._store.get_run, run_id)
            if record is None:  # pragma: no cover - only if the row vanished
                raise
            return RunHandle(run_id=run_id, thread_id=record.thread_id, created=False)

        if self._queue is not None:
            # Durable first, then the nudge. A client may replay from zero the
            # moment the start response arrives, so RUN_STARTED is committed
            # before the run is offered to any worker.
            await asyncio.to_thread(
                self._store.append_event,
                run_id,
                agui.run_started(thread_id=thread_id, run_id=run_id),
            )
            try:
                await self._queue.enqueue(run_id)
            except Exception:
                # The row is already committed and still ``queued``; the
                # sweeper re-sends it. Losing the nudge costs a wake-up, never
                # a run, so the request does not fail.
                logger.warning(
                    "could not enqueue %s; the sweeper will retry", run_id, exc_info=True
                )
            return RunHandle(run_id=run_id, thread_id=thread_id)

        active = _ActiveRun(
            run_id=run_id,
            thread_id=thread_id,
            buyer_id=buyer_id,
            session_id=session_id,
            text=text,
        )
        self._runs[run_id] = active
        # RUN_STARTED is durable before the task exists, so a client that
        # receives the start response can immediately replay from zero.
        await self._append(active, agui.run_started(thread_id=thread_id, run_id=run_id))
        active.task = asyncio.create_task(self._execute(active))
        return RunHandle(run_id=run_id, thread_id=thread_id)

    async def cancel(self, run_id: str) -> RunRecord:
        record = await asyncio.to_thread(self._store.get_run, run_id)
        if record is None:
            raise RunNotFound(run_id)
        if record.status in TERMINAL_STATUSES:
            return record
        if self._queue is not None:
            return await self._cancel_queued(record)
        active = self._runs.get(run_id)
        if active is None or active.done or active.terminal or active.cancelling:
            return record
        active.cancelling = True
        if active.task is not None:
            active.task.cancel()
        # A task cancelled before it ever started never enters ``_execute``, so
        # its own CancelledError handler cannot write the terminal event.
        await asyncio.sleep(0)
        if active.task is not None and active.task.done() and not active.done:
            await self._append(active, agui.state_snapshot(snapshot(None, "cancelled")))
            await self._append(
                active,
                agui.run_finished(
                    thread_id=active.thread_id,
                    run_id=active.run_id,
                    result={"status": "cancelled"},
                ),
                status="cancelled",
            )
            active.terminal = True
            await self._finish(active, "cancelled")
        refreshed = await asyncio.to_thread(self._store.get_run, run_id)
        return refreshed or record

    async def _cancel_queued(self, record: RunRecord) -> RunRecord:
        """Cancel in one of the two ways a queued or running run allows.

        A queued run has no writer, so this process may end it. A running run
        belongs to a worker: only the process holding the event log's write
        order may narrate its ending, so it gets a flag instead and the next
        frame it writes is the last.
        """
        ended = await asyncio.to_thread(self._store.request_cancel, record.run_id)
        if ended:
            await asyncio.to_thread(
                self._store.append_event,
                record.run_id,
                agui.state_snapshot(snapshot(None, "cancelled")),
            )
            await asyncio.to_thread(
                self._store.append_event,
                record.run_id,
                agui.run_finished(
                    thread_id=record.thread_id,
                    run_id=record.run_id,
                    result={"status": "cancelled"},
                ),
                status="cancelled",
            )
        refreshed = await asyncio.to_thread(self._store.get_run, record.run_id)
        return refreshed or record

    async def shutdown(self) -> None:
        """Stop in-flight turns and mark them interrupted (not user-cancelled)."""
        self._shutting_down = True
        if self._queue is not None:
            # Rows belong to workers now; a restart here strands nothing.
            return
        tasks = [active.task for active in self._runs.values() if active.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
        # Tasks cancelled before their coroutine started leave the row
        # ``running``; close those books too so a replay has a terminal state.
        orphans = await asyncio.to_thread(self._store.interrupt_running)
        for run_id in orphans:
            await asyncio.to_thread(
                self._store.append_event,
                run_id,
                agui.run_error("server_restarted", code="RUN_INTERRUPTED"),
            )

    # ----------------------------------------------------------------- events

    async def events(
        self, run_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[tuple[int, dict] | None]:
        """Replay the durable log, then follow live events.

        ``None`` is a heartbeat; the SSE layer turns it into a comment frame.
        The generator returns after the terminal event, so a finished run's
        stream closes by itself.
        """
        record = await asyncio.to_thread(self._store.get_run, run_id)
        if record is None:
            raise RunNotFound(run_id)

        if self._queue is not None:
            async for item in self._follow_journal(run_id, after_seq):
                yield item
            return

        active = self._runs.get(run_id)
        queue: asyncio.Queue | None = None
        if active is not None:
            # The lock is shared with ``_append``: an event is either in the
            # replay we are about to read or in the queue we are about to join.
            async with active.lock:
                replay = await asyncio.to_thread(
                    self._store.list_events, run_id, after_seq=after_seq
                )
                if not active.done:
                    queue = asyncio.Queue(maxsize=self._buffer)
                    active.subscribers.add(queue)
        else:
            replay = await asyncio.to_thread(self._store.list_events, run_id, after_seq=after_seq)

        try:
            for item in replay:
                yield item
            if queue is None:
                return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=self._heartbeat)
                except TimeoutError:
                    yield None
                    continue
                if item is _CLOSED:
                    return
                yield item
        finally:
            if queue is not None and active is not None:
                async with active.lock:
                    active.subscribers.discard(queue)

    async def _follow_journal(
        self, run_id: str, after_seq: int
    ) -> AsyncIterator[tuple[int, dict] | None]:
        """Tail the log the worker is writing.

        Polling looks like the cheap option and is the robust one: there is no
        handshake to lose, so a Redis outage, an API restart or a follower that
        joined late all cost the same — the next poll picks up where the last
        one stopped. Two readers need no coordination either.
        """
        last_seq = after_seq
        last_beat = time.monotonic()
        while True:
            events = await asyncio.to_thread(self._store.list_events, run_id, after_seq=last_seq)
            for seq, event in events:
                last_seq = seq
                last_beat = time.monotonic()
                yield (seq, event)
                if event.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                    return
            if events:
                continue
            record = await asyncio.to_thread(self._store.get_run, run_id)
            if record is None:  # pragma: no cover - the row does not disappear
                raise RunNotFound(run_id)
            if record.status in TERMINAL_STATUSES:
                # The empty poll and the status read are two statements, so
                # they are not one snapshot: the worker may have written the
                # rest of the journal in between. A terminal status means the
                # log is final, so drain it once before closing — returning
                # here would drop the ending for a reader who arrived a
                # moment too early.
                for seq, event in await asyncio.to_thread(
                    self._store.list_events, run_id, after_seq=last_seq
                ):
                    last_seq = seq
                    yield (seq, event)
                return
            await asyncio.sleep(self._follow_poll)
            if time.monotonic() - last_beat >= self._heartbeat:
                last_beat = time.monotonic()
                yield None

    async def get_run(self, run_id: str) -> RunRecord:
        record = await asyncio.to_thread(self._store.get_run, run_id)
        if record is None:
            raise RunNotFound(run_id)
        return record

    # --------------------------------------------------------------- internals

    async def _append(
        self,
        active: _ActiveRun,
        event: dict,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> int:
        async with active.lock:
            seq = await asyncio.to_thread(
                self._store.append_event, active.run_id, event, status=status, error=error
            )
            if status in TERMINAL_STATUSES:
                # The ending is durable; from here the run is over on the wire.
                active.terminal = True
                active.terminal_status = status
                active.terminal_error = error
            for queue in list(active.subscribers):
                _offer(queue, (seq, event))
            return seq

    def _mark_done(self, active: _ActiveRun) -> None:
        """Close the run to subscribers, atomically.

        Deliberately synchronous: with no ``await`` inside, no coroutine can
        interleave between flipping ``done`` and draining the queues, so the
        pair is atomic without ``active.lock`` — and therefore safe to call
        from a cancelling task, where taking the lock could itself block.
        """
        active.done = True
        for queue in list(active.subscribers):
            _offer(queue, _CLOSED)
        active.subscribers.clear()
        self._runs.pop(active.run_id, None)

    async def _finish(self, active: _ActiveRun, status: str, error: str | None = None) -> None:
        if active.done:
            return
        write = asyncio.create_task(
            asyncio.to_thread(self._store.finish_run, active.run_id, status=status, error=error)
        )
        try:
            # Shielding keeps a cleanup cancellation from stranding the row in
            # ``running``: a replay would then see a finished run that the
            # store still calls live, and startup would append a second ending.
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # The write is already in flight; let it land before unwinding.
            # ``write`` is a Task, so re-awaiting it is allowed (a bare
            # ``to_thread`` coroutine would raise "already awaited").
            await write
            self._mark_done(active)
            raise
        self._mark_done(active)

    async def _execute(self, active: _ActiveRun) -> None:
        """Run the turn, fanning each durable event out to live subscribers.

        The sequence itself lives in ``executor.execute_turn`` (shared with the
        S15 worker); this method owns only the in-memory half — notifying
        subscribers, and the shielded status write that lets a cancellation
        land without stranding the row in ``running``.
        """
        try:
            status = await execute_turn(
                run_id=active.run_id,
                thread_id=active.thread_id,
                buyer_id=active.buyer_id,
                session_id=active.session_id,
                text=active.text,
                turn_runner=self._turn_runner,
                append=partial(self._append, active),
                id_factory=self._id_factory,
                shutting_down=lambda: self._shutting_down,
            )
        except asyncio.CancelledError:
            if active.done:
                return
            if active.terminal_status is not None:
                await self._finish(active, active.terminal_status, error=active.terminal_error)
            else:
                # Cancelled before any write landed, so there is no ending on
                # the wire to preserve and no row left to reconcile.
                self._mark_done(active)
            return
        await self._finish(active, status)
