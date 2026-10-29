from datetime import UTC, datetime

from fastapi import APIRouter, Request

from harbor.core.config import Settings
from harbor.features.health.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        llm_configured=settings.llm_configured,
        time=datetime.now(UTC),
    )
