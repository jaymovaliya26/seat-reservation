import asyncio
import uuid
from collections import Counter

import asyncpg
import httpx
import pytest

from tests.conftest import create_show, user_headers


async def reserve(
    client: httpx.AsyncClient,
    show_id: str,
    user: str,
    seats: list[str],
    key: str | None = None,
    header_key: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {"seats": seats}
    if key is not None:
        body["idempotency_key"] = key
    headers = user_headers(user)
    if header_key is not None:
        headers["Idempotency-Key"] = header_key
    return await client.post(f"/shows/{show_id}/reserve", json=body, headers=headers)


@pytest.fixture
def k() -> str:
    """A per-test prefix for idempotency keys. Keys are scoped per user across all shows, and
    the tests share one database, so reusing a literal key between tests would (correctly) 409."""
    return f"{uuid.uuid4().hex[:8]}-"


async def _reservation_count(db: asyncpg.Connection, show_id: str) -> int:
    count: int = await db.fetchval(
        "SELECT count(*) FROM reservations WHERE show_id = $1", uuid.UUID(show_id)
    )
    return count


async def test_retry_returns_the_original_reservation(
    client: httpx.AsyncClient, k: str, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, ["A1", "A2"])

    first = await reserve(client, show_id, "alice", ["A1"], key=k + "order-1")
    retry = await reserve(client, show_id, "alice", ["A1"], key=k + "order-1")

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.headers["idempotent-replayed"] == "true"
    assert retry.json() == first.json()
    assert await _reservation_count(db, show_id) == 1


async def test_same_key_with_different_seats_is_409(
    client: httpx.AsyncClient, k: str, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    first = await reserve(client, show_id, "alice", ["A1"], key=k + "order-1")

    response = await reserve(client, show_id, "alice", ["A2"], key=k + "order-1")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_reused"
    assert error["reservation_id"] == first.json()["reservation_id"]
    seats = (await client.get(f"/shows/{show_id}")).json()["seats"]
    assert {"label": "A2", "status": "available"} in seats  # nothing moved
    assert await _reservation_count(db, show_id) == 1


async def test_same_key_for_another_show_is_409(client: httpx.AsyncClient, k: str) -> None:
    show_a = await create_show(client, ["A1"])
    show_b = await create_show(client, ["A1"])
    await reserve(client, show_a, "alice", ["A1"], key=k + "order-1")

    response = await reserve(client, show_b, "alice", ["A1"], key=k + "order-1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reused"


async def test_seat_order_does_not_change_the_request(client: httpx.AsyncClient, k: str) -> None:
    show_id = await create_show(client, ["A12", "A13"])
    first = await reserve(client, show_id, "alice", ["A13", "A12"], key=k + "pair")
    retry = await reserve(client, show_id, "alice", ["A12", "A13"], key=k + "pair")
    assert (first.status_code, retry.status_code) == (201, 200)


async def test_keys_are_scoped_per_user(client: httpx.AsyncClient, k: str) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    alice = await reserve(client, show_id, "alice", ["A1"], key=k + "same-key")
    bob = await reserve(client, show_id, "bob", ["A2"], key=k + "same-key")
    assert (alice.status_code, bob.status_code) == (201, 201)
    assert bob.json()["user_id"] == "bob"


async def test_key_in_header_works_and_must_agree_with_body(
    client: httpx.AsyncClient, k: str
) -> None:
    show_id = await create_show(client, ["A1", "A2"])

    first = await reserve(client, show_id, "alice", ["A1"], header_key=k + "hdr-1")
    retry = await reserve(client, show_id, "alice", ["A1"], key=k + "hdr-1")
    disagree = await reserve(client, show_id, "alice", ["A2"], key=k + "one", header_key=k + "two")

    assert (first.status_code, retry.status_code) == (201, 200)
    assert disagree.status_code == 422


async def test_a_declined_request_does_not_burn_its_key(client: httpx.AsyncClient, k: str) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    await reserve(client, show_id, "bob", ["A1"])

    declined = await reserve(client, show_id, "alice", ["A1"], key=k + "k")
    assert declined.status_code == 409

    # Nothing was stored for "k", so the client may use it for its next attempt.
    retry = await reserve(client, show_id, "alice", ["A1"], key=k + "k")
    assert retry.json()["error"]["code"] == "seat_taken"


async def test_without_a_key_each_request_is_new(client: httpx.AsyncClient, k: str) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    assert (await reserve(client, show_id, "alice", ["A1"])).status_code == 201
    assert (await reserve(client, show_id, "alice", ["A2"])).status_code == 201


async def test_50_parallel_retries_book_exactly_once(
    client: httpx.AsyncClient, k: str, db: asyncpg.Connection
) -> None:
    show_id = await create_show(client, ["A1", "A2", "A3"])

    responses = await asyncio.gather(
        *(reserve(client, show_id, "alice", ["A1", "A2"], key=k + "storm") for _ in range(50))
    )

    assert Counter(r.status_code for r in responses) == {201: 1, 200: 49}
    assert len({r.json()["reservation_id"] for r in responses}) == 1
    assert await _reservation_count(db, show_id) == 1
    holdings = await db.fetchval(
        "SELECT seat_count FROM user_show_holdings WHERE user_id = 'alice' AND show_id = $1",
        uuid.UUID(show_id),
    )
    assert holdings == 2  # counted once, not fifty times
