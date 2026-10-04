"""Application factory.

Gunicorn runs `app.main:create_app()`; locally, `uvicorn app.main:create_app --factory`.
A factory keeps imports free of side effects, so tests can build an app with their own settings.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app import __version__
from app.api import admin, auth, health, metrics, reservations, shows
from app.api.openapi import DESCRIPTION, SWAGGER_UI_PARAMETERS, TAGS
from app.config import Settings
from app.db import Database
from app.errors import install_error_handlers
from app.migrate import run_migrations
from app.observability import metrics as app_metrics
from app.observability.log_budget import LogBudget
from app.observability.logging import configure_logging
from app.observability.middleware import RequestContextMiddleware
from app.services.catalog import ShowCatalog

log = structlog.get_logger(component="app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A worker accepts no requests until this block finishes, so it never serves traffic
        # against an old schema. If a migration fails, the worker fails to boot and the
        # platform restarts the container.
        db = Database(settings)
        await db.connect()
        await run_migrations(settings.database_url)
        app.state.db = db
        app.state.catalog = ShowCatalog(db.pool)
        background = [
            asyncio.create_task(app_metrics.sample_pool(db.pool)),
            asyncio.create_task(log_budget.flush_every_second()),
        ]
        log.info("startup_complete", version=__version__)
        try:
            yield
        finally:
            log.info("shutdown_started")
            for task in background:
                task.cancel()
            log_budget.flush()
            await db.close()

    app = FastAPI(
        title="Seat Reservation API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        swagger_ui_parameters=SWAGGER_UI_PARAMETERS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    log_budget = LogBudget(per_second=settings.log_request_lines_per_second)
    app.add_middleware(RequestContextMiddleware, log_budget=log_budget)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(shows.router)
    app.include_router(reservations.router)
    app.include_router(admin.router)
    app.include_router(metrics.router)
    return app
