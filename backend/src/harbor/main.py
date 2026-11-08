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
from harbor.features.chat.service import SessionStore, run_turn
from harbor.features.guardrails.router import router as guardrails_router
from harbor.features.health.router import router as health_router
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.router import router as orders_router
from harbor.features.orders.service import OrderService
from harbor.features.orders.store import SqliteOrderStore
from harbor.features.runs.router import router as agui_router
from harbor.features.runs.service import RunManager
from harbor.features.runs.store import SqliteRunStore
from harbor.observability import Observability, configure_observability
from harbor.observability.middleware import observability_middleware
from harbor.observability.router import router as observability_router

logger = logging.getLogger(__name__)


def build_run_manager(
    settings: Settings,
    chat_store: SessionStore | None,
    order_service: OrderService | None,
) -> RunManager | None:
    """Assemble the S14 run manager, or ``None`` when there is nothing to run.

    A run is the same turn as ``POST /commerce/intents``, given a lifecycle:
    the manager supplies the transport observer so tool traffic lands in the
    event log, and ``shop`` state is rebuilt from the structured response.
    """
    if chat_store is None or not settings.agui_enabled:
        return None

    async def turn_runner(
        buyer_id: str,
        session_id: str,
        text: str,
        on_tool_results,
        on_text_delta,
    ):
        return await run_turn(
            chat_store,
            buyer_id,
            session_id,
            text,
            timeout_seconds=settings.turn_timeout_seconds,
            order_service=order_service,
            on_tool_results=on_tool_results,
            on_text_delta=on_text_delta,
        )

    return RunManager(
        SqliteRunStore(settings.runs_db_path),
        turn_runner,
        subscriber_buffer=settings.run_event_buffer,
        heartbeat_seconds=settings.run_heartbeat_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "Starting %s %s (%s)",
        settings.app_name,
        settings.app_version,
        settings.app_env,
    )
    # S13: install the three observability lines before anything else runs, so
    # startup work and the first request both have a trace and a metric sink.
    observability = (
        Observability.create(
            trace_buffer=settings.observability_trace_buffer,
            event_buffer=settings.observability_event_buffer,
            metric_window=settings.observability_metric_window,
        )
        if settings.observability_enabled
        else None
    )
    previous_observability = configure_observability(observability)
    app.state.observability = observability
    # S6: the repository is cheap and always available; the chat store needs a model.
    repo = SessionRepository(settings.sessions_dir)
    app.state.session_repository = repo
    # S10: orders are application-layer state, so the endpoints work even when
    # no model is configured. Orders and evidence share one SQLite file.
    order_service = OrderService(
        SqliteOrderStore(settings.orders_db_path),
        SqliteEvidenceStore(
            settings.orders_db_path, retention_days=settings.evidence_retention_days
        ),
    )
    app.state.order_service = order_service
    if settings.llm_configured:
        logger.info("Assembling shared runtime + chat session store…")
        runtime = build_runtime(settings)
        app.state.chat_store = SessionStore(
            agent_factory=lambda state: build_agent(
                runtime, state=state, order_service=order_service
            ),
            repository=repo,
            guard=runtime.guard,
        )
        # Exposed for introspection/observability and for the fault-injection
        # smoke test (verify_s5 patches runtime.service.search to force 502).
        app.state.runtime = runtime
    else:
        app.state.chat_store = None
        app.state.runtime = None
    # S14: runs are a transport over the same turn; without a chat store the
    # routes stay mounted and report 503 rather than pretending to work.
    run_manager = build_run_manager(settings, app.state.chat_store, app.state.order_service)
    app.state.run_manager = run_manager
    if run_manager is not None:
        # A previous process may have died mid-run; close those books before
        # this one starts accepting subscribers.
        await run_manager.startup()
    yield
    if run_manager is not None:
        await run_manager.shutdown()
    # Restore whatever instance was active before this app assembled, so nested
    # TestClients or embedded apps do not keep a shutdown instance installed.
    configure_observability(previous_observability)
    # The router must not keep serving a shut-down instance's data.
    app.state.observability = None
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
    # Set before the lifespan runs: until this app installs its own instance,
    # the observability routes report disabled rather than borrowing another
    # app's process-global instance.
    app.state.observability = None
    # Until the lifespan assembles one, the AG-UI routes report 503 rather
    # than failing on a missing attribute.
    app.state.run_manager = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    # S13: correlate every request with a trace id (incoming X-Request-ID wins)
    # and expose the trace back to the caller on the response.
    app.middleware("http")(observability_middleware)
    # The S5 checklist contract is root-level (/commerce/intents, /health), while
    # the existing frontend + tests use the /api prefix for health. Mount health
    # under both, and the commerce endpoint at its checklist path.
    app.include_router(health_router, prefix=cfg.api_prefix)
    app.include_router(health_router)
    app.include_router(commerce_router, prefix="/commerce")
    # S10: the human gate. These routes live beside /commerce/intents so the
    # buyer's app can confirm the draft the chat turn just produced.
    app.include_router(orders_router, prefix="/commerce")
    # S11: independent of ``llm_configured`` — with no runtime it reports
    # ``enabled: false`` rather than 404, so ops can tell "off" from "missing".
    app.include_router(guardrails_router, prefix=cfg.api_prefix)
    # S13: trace, metrics and the event stream. Independent of the runtime, so
    # a deployment without a model still reports its own health.
    app.include_router(observability_router, prefix=cfg.api_prefix)
    # S14: AG-UI transport. The checklist's paths are root-level, so the
    # frontend talks to ``/agui/...`` beside ``/commerce/...``.
    app.include_router(agui_router)

    return app
