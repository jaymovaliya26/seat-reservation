import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from app.migrate import MigrationError, load_migrations, run_migrations


async def _applied_versions(database_url: str) -> list[str]:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    finally:
        await conn.close()
    return [row["version"] for row in rows]


async def test_applies_pending_migrations_once(empty_database_url: str) -> None:
    every_version = [m.version for m in load_migrations()]

    assert await run_migrations(empty_database_url) == every_version
    assert await run_migrations(empty_database_url) == []
    assert await _applied_versions(empty_database_url) == every_version


async def test_concurrent_startups_apply_each_migration_exactly_once(
    empty_database_url: str,
) -> None:
    # Eight workers booting at the same moment, as Gunicorn does.
    results = await asyncio.gather(*(run_migrations(empty_database_url) for _ in range(8)))

    applied_by_anyone = sorted(version for result in results for version in result)
    assert applied_by_anyone == [m.version for m in load_migrations()]
    assert sum(1 for result in results if result) == 1


async def test_editing_an_applied_migration_is_refused(
    empty_database_url: str, tmp_path: Path
) -> None:
    migration = tmp_path / "0001_widgets.sql"
    migration.write_text("CREATE TABLE widgets (id int PRIMARY KEY);")
    await run_migrations(empty_database_url, tmp_path)

    migration.write_text("CREATE TABLE widgets (id bigint PRIMARY KEY);")
    with pytest.raises(MigrationError, match="0001_widgets was edited"):
        await run_migrations(empty_database_url, tmp_path)


async def test_failed_migration_leaves_nothing_behind(
    empty_database_url: str, tmp_path: Path
) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok (id int);")
    (tmp_path / "0002_broken.sql").write_text(
        "CREATE TABLE half_done (id int); SELECT * FROM no_such_table;"
    )

    with pytest.raises(asyncpg.UndefinedTableError):
        await run_migrations(empty_database_url, tmp_path)

    assert await _applied_versions(empty_database_url) == ["0001_ok"]
    conn = await asyncpg.connect(empty_database_url)
    try:
        assert await conn.fetchval("SELECT to_regclass('half_done')") is None
    finally:
        await conn.close()


class TestSchemaGuards:
    """The database itself refuses states that would mean a double sale or a broken count."""

    @pytest.fixture
    async def db(
        self, empty_database_url: str
    ) -> AsyncIterator[tuple[asyncpg.Connection, uuid.UUID]]:
        await run_migrations(empty_database_url)
        conn = await asyncpg.connect(empty_database_url)
        show_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)"
            " VALUES ($1, 'test', 25000, 4, 1)",
            show_id,
        )
        await conn.execute(
            "INSERT INTO seats (show_id, label, position) VALUES ($1, 'A12', 1)", show_id
        )
        yield conn, show_id
        await conn.close()

    async def test_a_seat_cannot_exist_twice(
        self, db: tuple[asyncpg.Connection, uuid.UUID]
    ) -> None:
        conn, show_id = db
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO seats (show_id, label, position) VALUES ($1, 'A12', 2)", show_id
            )

    async def test_a_confirmed_seat_must_have_an_owner(
        self, db: tuple[asyncpg.Connection, uuid.UUID]
    ) -> None:
        conn, show_id = db
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE seats SET status = 'confirmed' WHERE show_id = $1", show_id)

    async def test_an_available_seat_cannot_keep_an_owner(
        self, db: tuple[asyncpg.Connection, uuid.UUID]
    ) -> None:
        conn, show_id = db
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE seats SET user_id = 'alice' WHERE show_id = $1", show_id)

    async def test_price_must_be_positive_integer_paise(
        self, db: tuple[asyncpg.Connection, uuid.UUID]
    ) -> None:
        conn, _ = db
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)"
                " VALUES ($1, 'free', 0, 4, 1)",
                uuid.uuid4(),
            )
