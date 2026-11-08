"""The S15 wake-up channel (Redis Streams), and its in-memory twin for tests.

Redis is deliberately *not* where run state lives: every message carries only
a run id, and the worker answers it by reading the SQLite journal. Losing a
message therefore costs one wake-up, which the sweeper re-sends from the
journal — it never costs a run.

Streams (rather than ``LPUSH``/``BRPOP``) buy the same thing for the same
price: a consumer group acknowledges what it finished, so a message that was
delivered but not acknowledged comes back after a timeout instead of vanishing
with its worker.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["Claimed", "MemoryRunQueue", "RedisRunQueue", "RunQueue"]


@dataclass(frozen=True)
class Claimed:
    """One delivery of one run id."""

    message_id: str
    run_id: str


class RunQueue(Protocol):
    async def enqueue(self, run_id: str) -> None: ...

    async def claim(self, consumer: str, *, block_ms: int = 5000) -> Claimed | None: ...

    async def ack(self, message_id: str) -> None: ...

    async def reclaim(
        self, consumer: str, *, min_idle_ms: int, count: int = 10
    ) -> list[Claimed]: ...

    async def close(self) -> None: ...


class MemoryRunQueue:
    """The reference behaviour: delivered once, redelivered when not acked."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._runs: dict[str, str] = {}
        self._inflight: dict[str, tuple[str, float]] = {}
        self._counter = 0
        self._clock = clock

    async def enqueue(self, run_id: str) -> None:
        self._counter += 1
        message_id = f"{self._counter}-0"
        self._runs[message_id] = run_id
        self._ready.put_nowait(message_id)

    async def claim(self, consumer: str, *, block_ms: int = 5000) -> Claimed | None:
        timeout = block_ms / 1000 if block_ms > 0 else 0
        try:
            message_id = await asyncio.wait_for(self._ready.get(), timeout=timeout)
        except TimeoutError:
            return None
        self._inflight[message_id] = (consumer, self._clock())
        return Claimed(message_id, self._runs[message_id])

    async def ack(self, message_id: str) -> None:
        self._inflight.pop(message_id, None)
        self._runs.pop(message_id, None)

    async def reclaim(self, consumer: str, *, min_idle_ms: int, count: int = 10) -> list[Claimed]:
        now = self._clock()
        claimed: list[Claimed] = []
        for message_id, (_owner, delivered_at) in list(self._inflight.items()):
            if len(claimed) >= count:
                break
            if (now - delivered_at) * 1000 < min_idle_ms:
                continue
            self._inflight[message_id] = (consumer, now)
            claimed.append(Claimed(message_id, self._runs[message_id]))
        return claimed

    async def close(self) -> None:
        return None


class RedisRunQueue:
    """``XADD`` to wake a worker; ``XACK``/``XAUTOCLAIM`` so a crash is noticed."""

    def __init__(
        self,
        url: str | None = None,
        *,
        stream: str = "harbor:runs",
        group: str = "harbor-workers",
        client: Any | None = None,
    ) -> None:
        self._stream = stream
        self._group = group
        if client is not None:
            self._client = client
        else:
            # Imported here so the memory path (tests, inline mode) never needs
            # the package at import time.
            import redis.asyncio as redis_async

            self._client = redis_async.from_url(
                url or "redis://localhost:6379/0", decode_responses=True
            )
        self._group_ready = False

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            # ``MKSTREAM`` creates the stream, so the group exists before the
            # first message does. Without it, ``XREADGROUP`` on a missing
            # stream raises and the first run of a fresh deployment fails.
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP is the success case
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def enqueue(self, run_id: str) -> None:
        await self._ensure_group()
        await self._client.xadd(
            self._stream,
            {"run_id": run_id},
            # The journal is the truth; a trimmed message costs a wake-up,
            # never a run, so the stream may stay bounded.
            maxlen=10_000,
            approximate=True,
        )

    async def claim(self, consumer: str, *, block_ms: int = 5000) -> Claimed | None:
        await self._ensure_group()
        reply = await self._client.xreadgroup(
            self._group, consumer, {self._stream: ">"}, count=1, block=block_ms
        )
        if not reply:
            return None
        entries = reply[0][1]
        if not entries:
            return None
        message_id, fields = entries[0]
        run_id = fields.get("run_id")
        if not run_id:
            # An unknown payload can never be executed; dropping it keeps the
            # pending list from growing holes.
            await self.ack(message_id)
            return None
        return Claimed(message_id, run_id)

    async def ack(self, message_id: str) -> None:
        await self._client.xack(self._stream, self._group, message_id)

    async def reclaim(self, consumer: str, *, min_idle_ms: int, count: int = 10) -> list[Claimed]:
        await self._ensure_group()
        reply = await self._client.xautoclaim(
            self._stream,
            self._group,
            consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # redis-py has returned both a 2-tuple and a 3-tuple here depending on
        # version; only the entry list matters to us.
        entries = reply[1] if len(reply) >= 2 else []
        return [
            Claimed(message_id, fields["run_id"])
            for message_id, fields in entries or []
            if fields.get("run_id")
        ]

    async def close(self) -> None:
        await self._client.aclose()
