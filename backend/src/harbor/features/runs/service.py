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
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from harbor.features.runs import agui
from harbor.features.runs.store import RunExists, RunNotFound, RunRecord, SqliteRunStore

logger = logging.getLogger(__name__)

ToolResults = list[dict]
ToolObserver = Callable[[ToolResults], Awaitable[None]]
TextObserver = Callable[[str], Awaitable[None]]
TurnRunner = Callable[
    [str, str, str, ToolObserver | None, TextObserver],
    Awaitable[Any],
]

_CLOSED = object()

_ERRORS: dict[str, tuple[str, str]] = {
    "ModelTimeoutError": ("MODEL_TIMEOUT", "模型响应超时"),
    "ModelFailureError": ("MODEL_FAILED", "模型调用失败"),
    "ToolFailureError": ("TOOL_FAILED", "商品检索工具调用失败"),
    "NoStructuredOutputError": ("NO_STRUCTURED_OUTPUT", "模型未产出结构化输出"),
    "ContextCapacityError": ("CONTEXT_CAPACITY", "上下文超出容量限制"),
}


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


def _error_for(exc: BaseException) -> tuple[str, str]:
    code, message = _ERRORS.get(type(exc).__name__, ("TURN_FAILED", "本轮处理失败"))
    # The exception text can name filesystem paths or provider internals; it
    # stays in the server log, never in the buyer-facing stream.
    logger.warning("run turn failed (%s)", type(exc).__name__, exc_info=exc)
    return code, message


def _final_text(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, dict):
        return str(response.get("final_text") or "")
    return str(getattr(response, "final_text", "") or "")


def _snapshot(response: Any, status: str) -> dict[str, Any]:
    """The structured state a client rebuilds from; text is never parsed.

    Card fields keep the response's snake_case so the frontend can reuse the
    OpenAPI-generated ``ProductCard`` type unchanged.
    """
    if response is None:
        data: dict[str, Any] = {}
    elif hasattr(response, "model_dump"):
        data = response.model_dump(mode="json")
    elif isinstance(response, dict):
        data = response
    else:
        data = {}
    return {
        "status": status,
        "final_text": data.get("final_text", ""),
        "cards": data.get("cards", []),
        "pending_draft": data.get("pending_draft"),
    }


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
    cancelling: bool = False
    task: asyncio.Task | None = None


class RunManager:
    def __init__(
        self,
        store: SqliteRunStore,
        turn_runner: TurnRunner,
        *,
        subscriber_buffer: int = 500,
        heartbeat_seconds: float = 15.0,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._turn_runner = turn_runner
        self._buffer = subscriber_buffer
        self._heartbeat = heartbeat_seconds
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._runs: dict[str, _ActiveRun] = {}
        self._shutting_down = False

    # ------------------------------------------------------------- lifecycle

    async def startup(self) -> None:
        """Close the books on runs a previous process left ``running``."""
        self._shutting_down = False
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
            )
        except RunExists:
            record = await asyncio.to_thread(self._store.get_run, run_id)
            if record is None:  # pragma: no cover - only if the row vanished
                raise
            return RunHandle(run_id=run_id, thread_id=record.thread_id, created=False)

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
            await self._append(active, agui.state_snapshot(_snapshot(None, "cancelled")))
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
        return record

    async def shutdown(self) -> None:
        """Stop in-flight turns and mark them interrupted (not user-cancelled)."""
        self._shutting_down = True
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
        message_id = f"msg_{active.run_id}"
        text_open = False

        async def observer(records: ToolResults) -> None:
            for record in records:
                call_id = str(record.get("id") or f"call_{self._id_factory()[:8]}")
                name = str(record.get("name") or "tool")
                await self._append(
                    active,
                    agui.tool_call_start(call_id, name, parent_message_id=message_id),
                )
                arguments = record.get("arguments")
                if arguments:
                    await self._append(active, agui.tool_call_args(call_id, str(arguments)))
                await self._append(active, agui.tool_call_end(call_id))
                await self._append(
                    active,
                    agui.tool_call_result(
                        f"msg_{call_id}", call_id, str(record.get("output") or "")
                    ),
                )

        try:
            await self._append(active, agui.step_started("agent"))
            await self._append(active, agui.text_message_start(message_id))
            text_open = True

            async def on_text_delta(delta: str) -> None:
                # Persist-before-broadcast applies to narration too: a refresh
                # mid-turn must replay the same words the live stream showed.
                await self._append(active, agui.text_message_content(message_id, delta))

            response = await self._turn_runner(
                active.buyer_id, active.session_id, active.text, observer, on_text_delta
            )
            final_text = _final_text(response)
            if final_text:
                await self._append(active, agui.text_message_content(message_id, final_text))
            await self._append(active, agui.text_message_end(message_id))
            text_open = False
            await self._append(active, agui.state_snapshot(_snapshot(response, "finished")))
            await self._append(active, agui.step_finished("agent"))
            await self._append(
                active,
                agui.run_finished(
                    thread_id=active.thread_id,
                    run_id=active.run_id,
                    result={"status": "finished"},
                ),
                status="finished",
            )
            active.terminal = True
            await self._finish(active, "finished")
        except asyncio.CancelledError:
            if active.terminal or active.done:
                # The run already ended on the wire; this cancellation is
                # cleanup (a finished task being torn down), not a command.
                self._mark_done(active)
                return
            if text_open:
                await self._append(active, agui.text_message_end(message_id))
            if self._shutting_down:
                await self._append(
                    active,
                    agui.run_error("server_restarted", code="RUN_INTERRUPTED"),
                    status="interrupted",
                    error="server_restarted",
                )
                active.terminal = True
                await self._finish(active, "interrupted", error="server_restarted")
            else:
                await self._append(active, agui.state_snapshot(_snapshot(None, "cancelled")))
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
        except Exception as exc:  # noqa: BLE001 - every failure becomes RUN_ERROR
            if text_open:
                await self._append(active, agui.text_message_end(message_id))
            code, message = _error_for(exc)
            await self._append(
                active, agui.run_error(message, code=code), status="failed", error=code
            )
            active.terminal = True
            await self._finish(active, "failed", error=code)
