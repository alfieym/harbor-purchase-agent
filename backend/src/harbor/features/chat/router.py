"""``POST /commerce/intents`` + ``GET /commerce/sessions`` (S5/S6)."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from harbor.agents.context_governance import ContextCapacityError
from harbor.core.config import Settings
from harbor.features.chat.repository import (
    SessionRepository,
    SnapshotLoadError,
    SnapshotWriteError,
)
from harbor.features.chat.schemas import (
    CommerceIntentRequest,
    CommerceIntentResponse,
    SessionListResponse,
)
from harbor.features.chat.service import (
    ModelFailureError,
    ModelTimeoutError,
    NoStructuredOutputError,
    SessionStore,
    ToolFailureError,
    run_turn,
)

router = APIRouter(tags=["commerce"])


@router.post("/intents", response_model=CommerceIntentResponse)
async def intents(request: CommerceIntentRequest, http_request: Request) -> CommerceIntentResponse:
    store: SessionStore | None = http_request.app.state.chat_store
    if store is None:
        raise HTTPException(status_code=503, detail="模型未配置（缺少 OPENAI_API_KEY）")
    settings: Settings = http_request.app.state.settings
    try:
        return await run_turn(
            store,
            request.buyer_id,
            request.shopping_session_id,
            request.raw_query,
            timeout_seconds=settings.turn_timeout_seconds,
        )
    except ContextCapacityError as exc:
        raise HTTPException(status_code=507, detail="上下文超出容量闸门") from exc
    except ModelTimeoutError as exc:
        raise HTTPException(
            status_code=504, detail=f"模型调用超时（>{exc.timeout_seconds}s）"
        ) from exc
    except ModelFailureError as exc:
        raise HTTPException(status_code=502, detail="模型调用失败") from exc
    except ToolFailureError as exc:
        raise HTTPException(status_code=502, detail="商品检索工具调用失败") from exc
    except NoStructuredOutputError as exc:
        raise HTTPException(status_code=502, detail=f"模型未产出结构化输出：{exc.text}") from exc
    except SnapshotLoadError as exc:
        # S6-3: never silently start from scratch on a broken snapshot.
        raise HTTPException(status_code=500, detail="会话快照损坏") from exc
    except SnapshotWriteError as exc:
        raise HTTPException(status_code=500, detail="会话快照写入失败") from exc


@router.get("/sessions", response_model=SessionListResponse)
async def sessions(buyer_id: str, http_request: Request) -> SessionListResponse:
    """List one buyer's sessions, newest ``last_active_at`` first (S6-5)."""
    repo: SessionRepository | None = getattr(http_request.app.state, "session_repository", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="会话存储未配置")
    try:
        summaries = await asyncio.to_thread(repo.list_sessions, buyer_id)
    except SnapshotLoadError as exc:
        raise HTTPException(status_code=500, detail="会话快照损坏") from exc
    return SessionListResponse(sessions=summaries)
