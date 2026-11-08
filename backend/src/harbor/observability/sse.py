"""Server-sent events framing for the S13 event bus."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from harbor.observability.events import EventBus, UserEvent

__all__ = ["KEEPALIVE", "encode_sse", "sse_stream"]

KEEPALIVE = ": keepalive\n\n"


def encode_sse(event: UserEvent) -> str:
    """One SSE frame: id, event type and JSON data."""
    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


async def sse_stream(
    bus: EventBus,
    *,
    session_id: str | None = None,
    after_seq: int = 0,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Yield SSE frames; a heartbeat keeps idle proxies from closing the pipe."""
    subscription = bus.subscribe(session_id=session_id, after_seq=after_seq)
    try:
        while True:
            event = await subscription.get(timeout=heartbeat_seconds)
            yield KEEPALIVE if event is None else encode_sse(event)
    finally:
        subscription.close()
