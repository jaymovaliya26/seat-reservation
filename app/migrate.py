"""Forward-only SQL migrations, applied at startup.

Every Gunicorn worker (and every replica) calls `run_migrations` when it boots. A Postgres
advisory lock makes them take turns: the first applies what is pending, the rest find nothing
to do. Each file runs in its own transaction together with its bookkeeping row, so a crash
mid-migration leaves no half-applied file. A checksum stops an already-applied file from being
edited silently.

Run by hand with: python -m app.migrate
"""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import structlog

log = structlog.get_logger(component="migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Any fixed 64-bit number works; it only has to be the same for every process of this app.
ADVISORY_LOCK_KEY = 7_264_530_021


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    return [
        Migration(version=path.stem, sql=path.read_text())
        for path in sorted(directory.glob("*.sql"))
    ]


async def run_migrations(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the versions this call applied."""
    migrations = await asyncio.to_thread(load_migrations, directory)

    # A dedicated connection without the pool's statement_timeout: waiting for the lock while
    # another process migrates must not time out.
    conn = await asyncpg.connect(
        database_url,
        timeout=10,
        server_settings={"application_name": "seat-reservation-migrate", "statement_timeout": "0"},
    )
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            return await _apply_pending(conn, migrations)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()


async def _apply_pending(conn: asyncpg.Connection, migrations: list[Migration]) -> list[str]:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     text        PRIMARY KEY,
            checksum    text        NOT NULL,
            applied_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
    applied: dict[str, str] = {row["version"]: row["checksum"] for row in rows}

    newly_applied: list[str] = []
    for migration in migrations:
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                raise MigrationError(
                    f"Migration {migration.version} was edited after it was applied. "
                    "Add a new migration instead."
                )
            continue
        async with conn.transaction():
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                migration.version,
                migration.checksum,
            )
        log.info("migration_applied", version=migration.version)
        newly_applied.append(migration.version)
    return newly_applied


async def _main() -> None:
    from app.config import Settings
    from app.observability.logging import configure_logging

    settings = Settings()
    configure_logging(settings.log_level)
    applied = await run_migrations(settings.database_url)
    log.info("migrations_done", applied=applied)


if __name__ == "__main__":
    asyncio.run(_main())
