import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

from harbor.core.config import Settings
from harbor.features.health.schemas import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _database_status(request: Request) -> str:
    """Probe the order database; "unavailable" when this app never wired one."""
    order_service = getattr(request.app.state, "order_service", None)
    if order_service is None:
        return "unavailable"
    try:
        order_service.ping()
    except Exception:  # noqa: BLE001 - health must report, never raise
        logger.warning("Health probe failed for the order database", exc_info=True)
        return "error"
    return "ok"


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request, response: Response) -> HealthResponse:
    settings: Settings = request.app.state.settings
    # The session store is only built when the runtime (catalog + index + model)
    # assembled successfully, so its presence is our "catalog dependency" signal.
    catalog_loaded = getattr(request.app.state, "chat_store", None) is not None
    database = _database_status(request)
    degraded = database == "error"
    # A degraded dependency must not be reported as 200: a load balancer that
    # only reads the status code would keep sending traffic to it.
    response.status_code = 503 if degraded else 200
    return HealthResponse(
        status="degraded" if degraded else "ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        llm_configured=settings.llm_configured,
        model=settings.openai_model if settings.llm_configured else None,
        dependencies={
            "llm": "ok" if settings.llm_configured else "unconfigured",
            "catalog": "ok" if catalog_loaded else "unloaded",
            "database": database,
        },
        time=datetime.now(UTC),
    )
