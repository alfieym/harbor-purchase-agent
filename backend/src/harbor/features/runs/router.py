"""AG-UI transport (S14): start a run, follow it, cancel it, rebuild it.

The routes are thin on purpose. A run is started before the response is
written, so a client that receives ``runId`` can immediately replay from zero
even if the model is still thinking. ``GET .../events`` is the whole state
machine: replay from the durable log, then follow live events until the run's
terminal frame; a dropped connection costs the client a subscriber, nothing
more.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from harbor.features.runs.agui import encode_sse
from harbor.features.runs.schemas import (
    RunSnapshotResponse,
    RunStartRequest,
    RunStartResponse,
)
from harbor.features.runs.service import RunManager
from harbor.features.runs.store import RunNotFound

router = APIRouter(prefix="/agui", tags=["agui"])


def _manager(request: Request) -> RunManager:
    manager: RunManager | None = getattr(request.app.state, "run_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="run 未启用")
    return manager


@router.post("/runs", response_model=RunStartResponse, status_code=202)
async def start_run(body: RunStartRequest, request: Request) -> RunStartResponse:
    """202: the run exists and its ``RUN_STARTED`` is already durable."""
    handle = await _manager(request).start_run(
        buyer_id=body.buyer_id,
        session_id=body.shopping_session_id,
        text=body.latest_user_text or "",
        thread_id=body.thread_id,
        run_id=body.run_id,
    )
    return RunStartResponse(runId=handle.run_id, threadId=handle.thread_id)


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    manager = _manager(request)
    # EventSource reconnects with the id of the last frame it processed;
    # honouring it makes a refresh resume instead of replaying the whole run.
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None and last_event_id.isdigit():
        after = int(last_event_id)
    # Fail with 404 before the response starts: once the body is streaming,
    # the status line is already sent and cannot report a missing run.
    try:
        await manager.get_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc

    async def frames() -> AsyncIterator[str]:
        async for item in manager.events(run_id, after_seq=after):
            if item is None:
                yield ": heartbeat\n\n"
                continue
            seq, event = item
            yield encode_sse(seq, event)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, str]:
    """Cancellation is a command, never inferred from a closed connection."""
    try:
        record = await _manager(request).cancel(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return {"runId": record.run_id, "status": record.status}


@router.get("/runs/{run_id}", response_model=RunSnapshotResponse)
async def get_run(run_id: str, request: Request) -> RunSnapshotResponse:
    try:
        record = await _manager(request).get_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return RunSnapshotResponse(
        runId=record.run_id,
        threadId=record.thread_id,
        status=record.status,
        lastSeq=record.last_seq,
        error=record.error,
        createdAt=record.created_at,
        updatedAt=record.updated_at,
    )
