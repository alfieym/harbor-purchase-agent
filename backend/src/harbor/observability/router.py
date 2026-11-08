"""Read-only S13 observability endpoints.

``/traces`` answers "where did this turn's time go", ``/metrics`` answers "is
the system getting worse", and ``/events`` answers "what is this buyer seeing
right now" — the last one live over SSE, with replay for reconnects.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from harbor.core.config import Settings
from harbor.observability import Observability, get_observability
from harbor.observability.sse import sse_stream

router = APIRouter(prefix="/observability", tags=["observability"])


def _observability(request: Request) -> Observability | None:
    # An application that assembled without observability must report disabled
    # rather than silently falling back to another app's instance. ``create_app``
    # sets the attribute to ``None`` up front; the global is only consulted for
    # a bare app that never declared one (the CLI/embedded case).
    if hasattr(request.app.state, "observability"):
        return request.app.state.observability
    return get_observability()


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    instance = _observability(request)
    if instance is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "started_at": instance.started_at,
        "uptime_seconds": round(time.time() - instance.started_at, 3),
        "trace_count": len(instance.traces),
        "event_count": instance.events.size(),
        "metric_count": instance.metrics.name_count(),
    }


@router.get("/traces")
def list_traces(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    session_id: str | None = None,
) -> dict[str, Any]:
    instance = _observability(request)
    if instance is None:
        return {"enabled": False, "traces": []}
    return {
        "enabled": True,
        "traces": [
            trace.summary() for trace in instance.traces.recent(limit=limit, session_id=session_id)
        ],
    }


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, request: Request) -> dict[str, Any]:
    instance = _observability(request)
    trace = instance.traces.get(trace_id) if instance is not None else None
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return {"enabled": True, "trace": trace.to_dict()}


@router.get("/metrics")
def metrics(request: Request) -> dict[str, Any]:
    instance = _observability(request)
    if instance is None:
        return {"enabled": False, "metrics": {}}
    return {"enabled": True, "metrics": instance.metrics.snapshot()}


@router.get("/events")
def events(
    request: Request,
    session_id: str | None = None,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    instance = _observability(request)
    if instance is None:
        return {"enabled": False, "events": []}
    return {
        "enabled": True,
        "events": [
            event.to_dict()
            for event in instance.events.recent(
                session_id=session_id, after_seq=after_seq, limit=limit
            )
        ],
    }


@router.get("/events/stream")
async def stream_events(
    request: Request,
    session_id: str | None = None,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    instance = _observability(request)
    if instance is None:
        raise HTTPException(status_code=503, detail="可观测性未启用")
    # EventSource sends the last seen event id on reconnect; honouring it makes
    # a browser reconnect resume instead of replaying the whole buffer.
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None and last_event_id.isdigit():
        after_seq = int(last_event_id)
    settings: Settings | None = getattr(request.app.state, "settings", None)
    heartbeat = settings.observability_event_heartbeat_seconds if settings is not None else 15.0
    return StreamingResponse(
        sse_stream(
            instance.events,
            session_id=session_id,
            after_seq=after_seq,
            heartbeat_seconds=heartbeat,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
