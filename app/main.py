"""Application factory.

Gunicorn runs `app.main:create_app()`; locally, `uvicorn app.main:create_app --factory`.
A factory keeps imports free of side effects, so tests can build an app with their own settings.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app import __version__
from app.api import auth, health, reservations, shows
from app.config import Settings
from app.db import Database
from app.errors import install_error_handlers
from app.migrate import run_migrations
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
        log.info("startup_complete", version=__version__)
        try:
            yield
        finally:
            log.info("shutdown_started")
            await db.close()

    app = FastAPI(title="Seat Reservation", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(shows.router)
    app.include_router(reservations.router)
    return app
