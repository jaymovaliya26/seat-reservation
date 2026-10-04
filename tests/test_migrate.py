import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from app.migrate import MIGRATIONS_DIR, MigrationError, load_migrations, run_migrations


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


async def test_upgrading_a_v0_1_database_keeps_existing_bookings(
    empty_database_url: str, tmp_path: Path
) -> None:
    v0_1 = tmp_path / "v0.1"
    v0_1.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_init.sql", v0_1)
    await run_migrations(empty_database_url, v0_1)

    conn = await asyncpg.connect(empty_database_url)
    try:
        show_id, alice_res, bob_res = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)"
            " VALUES ($1, 'old', 100, 4, 3)",
            show_id,
        )
        await conn.execute(
            "INSERT INTO seats (show_id, label, position) VALUES ($1, 'A1', 1), ($1, 'A2', 2),"
            " ($1, 'A3', 3)",
            show_id,
        )
        for res_id, user, seats in ((alice_res, "alice", ["A1", "A2"]), (bob_res, "bob", ["A3"])):
            await conn.execute(
                "INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)"
                " VALUES ($1, $2, $3, $4, 100, 'confirmed')",
                res_id,
                show_id,
                user,
                seats,
            )
            await conn.execute(
                "UPDATE seats SET status = 'confirmed', reservation_id = $1, user_id = $2"
                " WHERE show_id = $3 AND label = ANY($4)",
                res_id,
                user,
                show_id,
                seats,
            )

        assert await run_migrations(empty_database_url) == [
            "0002_idempotency",
            "0003_user_show_holdings",
        ]

        holdings = dict(
            await conn.fetch("SELECT user_id, seat_count FROM user_show_holdings ORDER BY 1")
        )
        assert holdings == {"alice": 2, "bob": 1}
        keys = [r["idempotency_key"] for r in await conn.fetch("SELECT * FROM reservations")]
        assert len(set(keys)) == 2 and all(key.startswith("auto:") for key in keys)

        # A writer that predates keys (the old version during a rolling deploy) still works.
        await conn.execute(
            "INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)"
            " VALUES ($1, $2, 'carol', ARRAY['A9'], 100, 'confirmed')",
            uuid.uuid4(),
            show_id,
        )
    finally:
        await conn.close()
