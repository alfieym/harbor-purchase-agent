import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harbor.agents.factory import build_agent, build_runtime
from harbor.core.config import Settings, get_settings
from harbor.core.errors import install_error_handlers
from harbor.core.logging import setup_logging
from harbor.features.chat.repository import SessionRepository
from harbor.features.chat.router import router as commerce_router
from harbor.features.chat.service import SessionStore
from harbor.features.health.router import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "Starting %s %s (%s)",
        settings.app_name,
        settings.app_version,
        settings.app_env,
    )
    # S5: assemble the shared read-only runtime once at startup (products + index
    # + reranker + model), then hand out a fresh per-session Agent from the store.
    if settings.llm_configured:
        logger.info("Assembling shared runtime + chat session store…")
        runtime = build_runtime(settings)
        repo = SessionRepository(settings.sessions_dir)
        app.state.chat_store = SessionStore(
            agent_factory=lambda state: build_agent(runtime, state=state),
            repository=repo,
        )
        # Exposed for introspection/observability and for the fault-injection
        # smoke test (verify_s5 patches runtime.service.search to force 502).
        app.state.runtime = runtime
    else:
        app.state.chat_store = None
        app.state.runtime = None
    yield
    logger.info("Stopping %s", settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    setup_logging(cfg.log_level)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    # The S5 checklist contract is root-level (/commerce/intents, /health), while
    # the existing frontend + tests use the /api prefix for health. Mount health
    # under both, and the commerce endpoint at its checklist path.
    app.include_router(health_router, prefix=cfg.api_prefix)
    app.include_router(health_router)
    app.include_router(commerce_router, prefix="/commerce")

    return app
