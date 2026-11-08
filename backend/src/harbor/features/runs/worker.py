"""The S15 worker: claims a run id, then lets the journal decide what to do.

A queue message is a *nudge*, not a job. Nothing in it is trusted: the worker
re-reads the row, and only a row that is still ``queued`` may be executed.
That one rule turns at-least-once delivery into exactly-once execution without
a deduplication table, and it makes a duplicated message a no-op.

Two background jobs keep the system honest when nobody is watching:

* ``sweep`` interrupts runs whose worker stopped reporting in (a crashed
  process cannot write its own ending) and re-enqueues runs whose wake-up was
  lost.
* ``stop`` marks the runs this process was executing ``interrupted`` — a
  rolling deploy must not leave a row ``running`` for a process that is gone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from functools import partial
from typing import Any

from harbor.features.runs import agui
from harbor.features.runs.executor import (
    CLOSING_EVENT_TYPES,
    TurnCancelled,
    TurnRunner,
    execute_turn,
)
from harbor.features.runs.queue import Claimed, RunQueue
from harbor.features.runs.store import SqliteRunStore

logger = logging.getLogger(__name__)

__all__ = ["RunWorker", "default_consumer_name"]


def default_consumer_name() -> str:
    """Unique per process, so the pending list names who dropped a message."""
    return f"{socket.gethostname()}-{os.getpid()}"


class RunWorker:
    def __init__(
        self,
        store: SqliteRunStore,
        queue: RunQueue,
        turn_runner: TurnRunner,
        *,
        consumer: str | None = None,
        concurrency: int = 2,
        lease_seconds: float = 90.0,
        claim_idle_ms: int = 60_000,
        requeue_after_seconds: float = 15.0,
        sweep_seconds: float = 5.0,
        session_ttl_seconds: float = 300.0,
        block_ms: int = 5000,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._turn_runner = turn_runner
        self._consumer = consumer or default_consumer_name()
        self._concurrency = max(1, concurrency)
        self._lease = lease_seconds
        self._claim_idle_ms = claim_idle_ms
        self._requeue = requeue_after_seconds
        self._sweep_seconds = sweep_seconds
        self._session_ttl = session_ttl_seconds
        self._block_ms = block_ms
        self._id_factory = id_factory
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Claim until ``stop`` is called; returns once every loop has ended."""
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._claim_loop(), name=f"{self._consumer}-claim-{i}")
            for i in range(self._concurrency)
        ]
        self._tasks.append(asyncio.create_task(self._sweep_loop(), name=f"{self._consumer}-sweep"))
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for result in results:
            # Cancelled loops are how a normal stop ends; anything else is a
            # defect worth shouting about.
            if isinstance(result, Exception):
                logger.error("worker loop exited with an error", exc_info=result)

    async def stop(self) -> None:
        """Stop claiming and mark this process's in-flight runs interrupted."""
        self._stopping.set()
        tasks, self._tasks = list(self._tasks), []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._queue.close()

    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                batch = await self._next_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not read from the queue")
                await asyncio.sleep(1.0)
                continue
            for claimed in batch:
                if self._stopping.is_set():
                    return
                try:
                    await self.handle(claimed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Unacknowledged on purpose: the message returns after the
                    # idle timeout, and the journal decides what happens then.
                    logger.exception("worker failed while handling %s", claimed.run_id)

    async def _sweep_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sweep failed; the next one will retry")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._sweep_seconds)
            except TimeoutError:
                continue

    async def _next_batch(self) -> list[Claimed]:
        # A fresh message first; otherwise adopt entries a dead consumer left
        # pending, which is how a crashed worker's last message gets retried.
        claimed = await self._queue.claim(self._consumer, block_ms=self._block_ms)
        if claimed is not None:
            return [claimed]
        return await self._queue.reclaim(self._consumer, min_idle_ms=self._claim_idle_ms)

    # ------------------------------------------------------------------ work

    async def handle(self, claimed: Claimed) -> str | None:
        """Execute one delivery, or acknowledge it as somebody else's business.

        Returns the terminal status, or ``None`` when the journal said the run
        was not this message's to execute.
        """
        run_id = claimed.run_id
        record = await asyncio.to_thread(self._store.get_run, run_id)
        if record is None or record.status != "queued":
            # Unknown, already started, or already ended. Never re-execute a
            # run whose narration is on someone's screen.
            await self._queue.ack(claimed.message_id)
            return None

        session_id = record.shopping_session_id
        if not await asyncio.to_thread(
            self._store.acquire_session,
            session_id,
            run_id,
            self._consumer,
            ttl_seconds=self._session_ttl,
        ):
            # Another run of this conversation is executing. Acknowledge: the
            # sweeper re-sends this one once the session frees up.
            await self._queue.ack(claimed.message_id)
            logger.info("session %s is busy; %s waits for the sweeper", session_id, run_id)
            return None

        if not await asyncio.to_thread(
            self._store.try_claim, run_id, self._consumer, lease_seconds=self._lease
        ):
            # Somebody claimed it between our read and our write.
            await asyncio.to_thread(self._store.release_session, session_id, run_id)
            await self._queue.ack(claimed.message_id)
            return None

        try:
            status = await execute_turn(
                run_id=run_id,
                thread_id=record.thread_id,
                buyer_id=record.buyer_id,
                session_id=session_id,
                text=record.input_text,
                turn_runner=self._turn_runner,
                append=partial(self._append, run_id),
                id_factory=self._id_factory,
                shutting_down=lambda: self._stopping.is_set(),
            )
        finally:
            await asyncio.to_thread(self._store.release_session, session_id, run_id)
        await self._queue.ack(claimed.message_id)
        return status

    async def _append(
        self,
        run_id: str,
        event: dict,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> int:
        if status is None and event.get("type") not in CLOSING_EVENT_TYPES:
            # A cancel or a lost lease stops the turn before the next frame:
            # narration that keeps arriving after a cancel reads as a bug to
            # whoever asked for it to stop. Closing frames are exempt — they
            # are how the turn stops in a way a replay can follow.
            if await asyncio.to_thread(self._store.cancel_requested, run_id):
                raise TurnCancelled("cancel requested")
            if not await asyncio.to_thread(self._store.heartbeat, run_id, self._consumer):
                # The sweeper has already written this run's ending; the
                # terminal frame below would append after it, and a replay
                # stops at the first ending it sees.
                raise TurnCancelled("lease lost")
        return await asyncio.to_thread(
            self._store.append_event, run_id, event, status=status, error=error
        )

    # ---------------------------------------------------------------- sweeper

    async def sweep(self) -> None:
        """Reconcile the journal with reality; safe to run on every worker."""
        lost = await asyncio.to_thread(self._store.interrupt_stale, lease_seconds=self._lease)
        for run_id in lost:
            logger.warning("run %s lost its worker; marked interrupted", run_id)
            await asyncio.to_thread(
                self._store.append_event,
                run_id,
                agui.run_error("worker_lost", code="RUN_INTERRUPTED"),
            )

        stale = await asyncio.to_thread(self._store.stale_queued, seconds=self._requeue)
        for run_id in stale:
            try:
                await self._queue.enqueue(run_id)
            except Exception:
                # Redis may be down right now. The row is still queued, so the
                # next sweep tries again; nothing is lost.
                logger.warning(
                    "could not re-enqueue %s; retrying next sweep", run_id, exc_info=True
                )
                continue
            await asyncio.to_thread(self._store.mark_enqueued, run_id)

        await asyncio.to_thread(self._store.purge_expired_sessions)
