from datetime import UTC, datetime

from fastapi import APIRouter, Request

from harbor.core.config import Settings
from harbor.features.health.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    # The session store is only built when the runtime (catalog + index + model)
    # assembled successfully, so its presence is our "catalog dependency" signal.
    catalog_loaded = getattr(request.app.state, "chat_store", None) is not None
    return HealthResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        llm_configured=settings.llm_configured,
        model=settings.openai_model if settings.llm_configured else None,
        dependencies={
            "llm": "ok" if settings.llm_configured else "unconfigured",
            "catalog": "ok" if catalog_loaded else "unloaded",
        },
        time=datetime.now(UTC),
    )
