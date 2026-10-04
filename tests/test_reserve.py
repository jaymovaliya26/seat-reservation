import asyncio
import uuid

import asyncpg
import httpx
import pytest

from app.services.catalog import ShowCatalog
from tests.conftest import create_show, user_headers


async def reserve(
    client: httpx.AsyncClient, show_id: str, user: str, seats: list[str], **extra: object
) -> httpx.Response:
    return await client.post(
        f"/shows/{show_id}/reserve", json={"seats": seats, **extra}, headers=user_headers(user)
    )


async def test_reserve_confirms_the_seat(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A11", "A12", "A13"], price_paise=25_000)

    response = await reserve(client, show_id, "alice", ["A12"])

    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body["reservation_id"])
    assert body["show_id"] == show_id
    assert body["user_id"] == "alice"
    assert body["seats"] == ["A12"]
    assert body["amount_paise"] == 25_000
    assert body["status"] == "confirmed"

    show = (await client.get(f"/shows/{show_id}")).json()
    assert show["counts"] == {"total": 3, "available": 2, "held": 0, "confirmed": 1}
    assert {"label": "A12", "status": "confirmed"} in show["seats"]


async def test_multi_seat_amount_is_integer_paise(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["B1", "B2", "B3"], price_paise=33_333)
    body = (await reserve(client, show_id, "alice", ["B3", "B1"])).json()
    assert body["seats"] == ["B1", "B3"]
    assert body["amount_paise"] == 66_666


async def test_taken_seat_is_a_clean_409(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A12"])
    assert (await reserve(client, show_id, "alice", ["A12"])).status_code == 201

    response = await reserve(client, show_id, "bob", ["A12"])

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "seat_taken"
    assert error["seats"] == ["A12"]


async def test_partial_request_is_all_or_nothing(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, ["A12", "A13"])
    await reserve(client, show_id, "alice", ["A12"])

    response = await reserve(client, show_id, "bob", ["A12", "A13"])

    assert response.status_code == 409
    assert response.json()["error"]["seats"] == ["A12"]
    seats = (await client.get(f"/shows/{show_id}")).json()["seats"]
    assert {"label": "A13", "status": "available"} in seats  # bob got nothing
    reservations = await db.fetchval(
        "SELECT count(*) FROM reservations WHERE show_id = $1", uuid.UUID(show_id)
    )
    assert reservations == 1  # the declined attempt left no row behind


async def test_identity_comes_from_the_token_not_the_body(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, ["A1"])

    response = await reserve(client, show_id, "alice", ["A1"], user_id="bob")

    assert response.json()["user_id"] == "alice"
    owner = await db.fetchval(
        "SELECT user_id FROM seats WHERE show_id = $1 AND label = 'A1'", uuid.UUID(show_id)
    )
    assert owner == "alice"


async def test_unknown_seats_are_422(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    response = await reserve(client, show_id, "alice", ["A1", "Z9", "Z8"])
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_seats"
    assert error["seats"] == ["Z8", "Z9"]


async def test_unknown_show_is_404(client: httpx.AsyncClient) -> None:
    response = await reserve(client, str(uuid.uuid4()), "alice", ["A1"])
    assert response.status_code == 404


async def test_reserve_requires_a_token(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    response = await client.post(f"/shows/{show_id}/reserve", json={"seats": ["A1"]})
    assert response.status_code == 401


@pytest.mark.parametrize("seats", [[], ["A1", "A1"], ["not valid"]])
async def test_malformed_seat_lists_are_422(client: httpx.AsyncClient, seats: list[str]) -> None:
    show_id = await create_show(client, ["A1"])
    response = await reserve(client, show_id, "alice", seats)
    assert response.status_code == 422


class TestShowCatalog:
    async def test_concurrent_first_lookups_share_one_load(
        self, client: httpx.AsyncClient, database_url: str
    ) -> None:
        show_id = uuid.UUID(await create_show(client, ["A1", "A2"]))
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        try:
            catalog = ShowCatalog(pool)
            loads = 0
            original = catalog._load

            async def counting_load(sid: uuid.UUID):  # type: ignore[no-untyped-def]
                nonlocal loads
                loads += 1
                return await original(sid)

            catalog._load = counting_load  # type: ignore[method-assign]
            results = await asyncio.gather(*(catalog.get(show_id) for _ in range(200)))
        finally:
            await pool.close()

        assert loads == 1
        assert all(info is not None and info.seat_labels == {"A1", "A2"} for info in results)

    async def test_a_miss_is_not_cached(self, database_url: str) -> None:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            catalog = ShowCatalog(pool)
            show_id = uuid.uuid4()
            assert await catalog.get(show_id) is None

            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)"
                    " VALUES ($1, 'late', 100, 4, 1)",
                    show_id,
                )
                await conn.execute(
                    "INSERT INTO seats (show_id, label, position) VALUES ($1, 'A1', 1)", show_id
                )
            info = await catalog.get(show_id)
        finally:
            await pool.close()

        assert info is not None and info.seat_labels == {"A1"}
