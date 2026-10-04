"""Creating shows and reading their seat map."""

import uuid
from collections import Counter

import asyncpg

from app.models import SeatCounts, SeatOut, ShowOut


async def create_show(
    pool: asyncpg.Pool,
    *,
    name: str,
    seats: list[str],
    price_paise: int,
    per_user_limit: int,
) -> ShowOut:
    show_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        created_at = await conn.fetchval(
            """
            INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING created_at
            """,
            show_id,
            name,
            price_paise,
            per_user_limit,
            len(seats),
        )
        # One statement for the whole hall, in the order the seats were given.
        await conn.execute(
            """
            INSERT INTO seats (show_id, label, position)
            SELECT $1, label, position::int
              FROM unnest($2::text[]) WITH ORDINALITY AS s(label, position)
            """,
            show_id,
            seats,
        )
    return ShowOut(
        id=show_id,
        name=name,
        price_paise=price_paise,
        per_user_limit=per_user_limit,
        created_at=created_at,
        counts=SeatCounts(total=len(seats), available=len(seats), held=0, confirmed=0),
        seats=[SeatOut(label=label, status="available") for label in seats],
    )


async def get_show(pool: asyncpg.Pool, show_id: uuid.UUID) -> ShowOut | None:
    async with pool.acquire() as conn:
        show = await conn.fetchrow(
            "SELECT name, price_paise, per_user_limit, created_at FROM shows WHERE id = $1",
            show_id,
        )
        if show is None:
            return None
        # The counts come from this same single query, so they always describe one consistent
        # moment: available + held + confirmed == total, even mid-burst.
        rows = await conn.fetch(
            "SELECT label, status FROM seats WHERE show_id = $1 ORDER BY position", show_id
        )

    by_status = Counter(row["status"] for row in rows)
    return ShowOut(
        id=show_id,
        name=show["name"],
        price_paise=show["price_paise"],
        per_user_limit=show["per_user_limit"],
        created_at=show["created_at"],
        counts=SeatCounts(
            total=len(rows),
            available=by_status["available"],
            held=by_status["held"],
            confirmed=by_status["confirmed"],
        ),
        seats=[SeatOut(label=row["label"], status=row["status"]) for row in rows],
    )
