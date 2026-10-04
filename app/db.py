"""Postgres connection pools, and retries for transactions Postgres asks us to repeat."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import asyncpg
import structlog

from app.config import Settings
from app.observability import metrics

log = structlog.get_logger(component="db")

_CONNECT_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)

# Postgres aborts a transaction with one of these when it must break a deadlock or cannot
# serialize it. The transaction did nothing, so running it again is safe. Our fixed lock order
# should prevent both; this is the backstop that keeps a surprise from becoming a 500.
_RETRYABLE = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError)


async def with_retries[T](run: Callable[[], Awaitable[T]], attempts: int = 3) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await run()
        except _RETRYABLE as exc:
            if attempt == attempts:
                raise
            metrics.DB_TX_RETRIES.labels(sqlstate=exc.sqlstate).inc()
            log.warning("transaction_retry", attempt=attempt, sqlstate=exc.sqlstate)
            await asyncio.sleep(random.uniform(0.005, 0.02) * attempt)
    raise AssertionError("unreachable")


class Database:
    """Owns the asyncpg pools for one worker process.

    `pool` serves API traffic. `health_pool` is a separate two-connection pool used only by the
    readiness probe, so a saturated main pool cannot make a healthy database look down.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._health_pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool

    def _server_settings(self, application_name: str) -> dict[str, str]:
        s = self._settings
        return {
            "application_name": application_name,
            "statement_timeout": str(s.db_statement_timeout_ms),
            "lock_timeout": str(s.db_lock_timeout_ms),
            "idle_in_transaction_session_timeout": str(s.db_idle_in_transaction_timeout_ms),
        }

    async def connect(self) -> None:
        """Create both pools, retrying with backoff until Postgres accepts connections.

        On a cold start the database can come up after the app; we wait up to
        `db_startup_timeout_s` and then fail, so the platform restarts the container.
        """
        s = self._settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + s.db_startup_timeout_s
        delay = 0.25
        attempt = 0
        while True:
            attempt += 1
            try:
                self._pool = await asyncpg.create_pool(
                    s.database_url,
                    min_size=s.db_pool_min,
                    max_size=s.db_pool_max,
                    timeout=5,
                    server_settings=self._server_settings("seat-reservation"),
                )
                self._health_pool = await asyncpg.create_pool(
                    s.database_url,
                    min_size=1,
                    max_size=2,
                    timeout=5,
                    server_settings=self._server_settings("seat-reservation-health"),
                )
            except _CONNECT_ERRORS as exc:
                await self.close()
                if loop.time() + delay > deadline:
                    log.error("database_unreachable", attempts=attempt, error=repr(exc))
                    raise
                log.warning(
                    "database_not_ready", attempt=attempt, retry_in_s=delay, error=repr(exc)
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
            else:
                log.info("database_connected", attempts=attempt, pool_max=s.db_pool_max)
                return

    async def ping(self) -> bool:
        """True only if Postgres answers within the readiness timeout. Any failure fails closed."""
        if self._health_pool is None:
            return False
        try:
            async with asyncio.timeout(self._settings.readiness_timeout_s):
                value = await self._health_pool.fetchval("SELECT 1")
        except Exception:
            return False
        return bool(value == 1)

    async def close(self) -> None:
        for pool in (self._pool, self._health_pool):
            if pool is None:
                continue
            try:
                await asyncio.wait_for(pool.close(), timeout=10)
            except TimeoutError:
                pool.terminate()
        self._pool = None
        self._health_pool = None
