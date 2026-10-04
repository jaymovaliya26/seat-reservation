import asyncio
import uuid
from collections import Counter

import asyncpg
import httpx

from tests.conftest import create_show, user_headers


async def reserve(
    client: httpx.AsyncClient, show_id: str, user: str, seats: list[str]
) -> httpx.Response:
    return await client.post(
        f"/shows/{show_id}/reserve", json={"seats": seats}, headers=user_headers(user)
    )


async def _holdings(db: asyncpg.Connection, show_id: str, user: str) -> int | None:
    held: int | None = await db.fetchval(
        "SELECT seat_count FROM user_show_holdings WHERE user_id = $1 AND show_id = $2",
        user,
        uuid.UUID(show_id),
    )
    return held


async def test_limit_counts_across_reservations(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, [f"A{n}" for n in range(1, 10)], per_user_limit=4)

    assert (await reserve(client, show_id, "alice", ["A1", "A2", "A3"])).status_code == 201
    over = await reserve(client, show_id, "alice", ["A4", "A5"])
    assert over.status_code == 409
    error = over.json()["error"]
    assert error["code"] == "per_user_limit"
    assert (error["limit"], error["held"], error["requested"]) == (4, 3, 2)

    assert (await reserve(client, show_id, "alice", ["A4"])).status_code == 201
    assert (await reserve(client, show_id, "bob", ["A5", "A6", "A7", "A8"])).status_code == 201


async def test_one_request_over_the_limit_is_409(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, [f"A{n}" for n in range(1, 10)], per_user_limit=4)
    response = await reserve(client, show_id, "alice", ["A1", "A2", "A3", "A4", "A5"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "per_user_limit"


async def test_a_taken_seat_does_not_use_up_the_limit(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, [f"A{n}" for n in range(1, 10)], per_user_limit=4)
    await reserve(client, show_id, "bob", ["A9"])
    await reserve(client, show_id, "alice", ["A1", "A2", "A3"])

    taken = await reserve(client, show_id, "alice", ["A9"])

    assert taken.json()["error"]["code"] == "seat_taken"
    assert await _holdings(db, show_id, "alice") == 3  # the increment rolled back with the claim


async def test_10_parallel_requests_never_exceed_a_limit_of_4(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    seats = [f"A{n}" for n in range(1, 21)]
    show_id = await create_show(client, seats, per_user_limit=4)

    responses = await asyncio.gather(
        *(reserve(client, show_id, "greedy", [seat]) for seat in seats[:10])
    )

    assert Counter(r.status_code for r in responses) == {201: 4, 409: 6}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "per_user_limit"
    }
    assert await _holdings(db, show_id, "greedy") == 4
    owned = await db.fetchval(
        "SELECT count(*) FROM seats WHERE show_id = $1 AND user_id = 'greedy'", uuid.UUID(show_id)
    )
    assert owned == 4


async def test_parallel_multi_seat_requests_respect_the_limit(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    seats = [f"B{n}" for n in range(1, 31)]
    show_id = await create_show(client, seats, per_user_limit=4)
    pairs = [seats[i : i + 2] for i in range(0, 30, 2)]  # 15 requests of 2 seats each

    responses = await asyncio.gather(*(reserve(client, show_id, "greedy", p) for p in pairs))

    assert Counter(r.status_code for r in responses)[201] == 2
    assert await _holdings(db, show_id, "greedy") == 4


async def test_limit_is_per_show(client: httpx.AsyncClient) -> None:
    show_a = await create_show(client, ["A1", "A2"], per_user_limit=1)
    show_b = await create_show(client, ["A1", "A2"], per_user_limit=1)
    assert (await reserve(client, show_a, "alice", ["A1"])).status_code == 201
    assert (await reserve(client, show_b, "alice", ["A1"])).status_code == 201
    assert (await reserve(client, show_a, "alice", ["A2"])).status_code == 409
