"""Run a worker process: ``python -m harbor.worker``.

The worker is the API minus its HTTP surface: the same runtime, the same chat
store, the same turn. It watches one Redis stream, and everything else it does
— which run to execute, whether the run is still alive, how a dead worker's
run ends — it reads from the SQLite journal that the API already wrote.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from harbor.agents.factory import build_agent, build_runtime
from harbor.core.config import Settings, get_settings
from harbor.core.logging import setup_logging
from harbor.features.chat.repository import SessionRepository
from harbor.features.chat.service import SessionStore
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.service import OrderService
from harbor.features.orders.store import SqliteOrderStore
from harbor.features.runs.queue import RedisRunQueue
from harbor.features.runs.store import SqliteRunStore
from harbor.features.runs.worker import RunWorker, default_consumer_name
from harbor.runtime import build_turn_runner

logger = logging.getLogger(__name__)


def build_worker(settings: Settings) -> RunWorker:
    """Assemble everything one worker needs; no HTTP, no observability sink."""
    if not settings.llm_configured:
        raise RuntimeError("a worker cannot run a turn without a configured model")
    if not settings.queue_enabled:
        # Not fatal — a worker may be started before the API is flipped over —
        # but it means nothing will ever enqueue to this process.
        logger.warning("HARBOR_QUEUE_ENABLED is false; this worker will idle")

    runtime = build_runtime(settings)
    order_service = OrderService(
        SqliteOrderStore(settings.orders_db_path),
        SqliteEvidenceStore(
            settings.orders_db_path, retention_days=settings.evidence_retention_days
        ),
    )
    chat_store = SessionStore(
        agent_factory=lambda state: build_agent(runtime, state=state, order_service=order_service),
        repository=SessionRepository(settings.sessions_dir),
        guard=runtime.guard,
    )
    queue = RedisRunQueue(
        settings.redis_url,
        stream=settings.queue_stream,
        group=settings.queue_group,
    )
    return RunWorker(
        SqliteRunStore(settings.runs_db_path),
        queue,
        build_turn_runner(settings, chat_store, order_service),
        consumer=default_consumer_name(),
        concurrency=settings.worker_concurrency,
        lease_seconds=settings.run_lease_seconds,
        claim_idle_ms=settings.queue_claim_idle_ms,
        requeue_after_seconds=settings.run_requeue_after_seconds,
        sweep_seconds=settings.run_sweep_seconds,
    )


async def serve(worker: RunWorker) -> None:
    """Run until SIGINT/SIGTERM, then stop in-flight turns as interrupted."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stopping.set)
        except NotImplementedError:  # pragma: no cover - not reachable on POSIX
            signal.signal(signal_number, lambda *_: stopping.set())

    task = asyncio.create_task(worker.run(), name="harbor-worker")
    logger.info("worker is claiming runs")
    await stopping.wait()
    logger.info("stopping worker…")
    await worker.stop()
    await task


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    await serve(build_worker(settings))


if __name__ == "__main__":
    asyncio.run(main())
