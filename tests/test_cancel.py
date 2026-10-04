import asyncio
import uuid
from collections import Counter

import asyncpg
import httpx

from tests.conftest import create_show, user_headers


async def reserve(
    client: httpx.AsyncClient, show_id: str, user: str, seats: list[str], key: str | None = None
) -> httpx.Response:
    body: dict[str, object] = {"seats": seats}
    if key is not None:
        body["idempotency_key"] = key
    return await client.post(f"/shows/{show_id}/reserve", json=body, headers=user_headers(user))


async def cancel(client: httpx.AsyncClient, reservation_id: str, user: str) -> httpx.Response:
    return await client.post(f"/reservations/{reservation_id}/cancel", headers=user_headers(user))


async def seat_status(client: httpx.AsyncClient, show_id: str) -> dict[str, str]:
    show = (await client.get(f"/shows/{show_id}")).json()
    return {seat["label"]: seat["status"] for seat in show["seats"]}


async def test_cancel_returns_seats_to_sale(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2", "A3"])
    booked = (await reserve(client, show_id, "alice", ["A1", "A2"])).json()

    response = await cancel(client, booked["reservation_id"], "alice")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["seats"] == ["A1", "A2"]
    assert await seat_status(client, show_id) == {
        "A1": "available",
        "A2": "available",
        "A3": "available",
    }
    # Cleanly re-bookable by anyone.
    assert (await reserve(client, show_id, "bob", ["A1"])).status_code == 201


async def test_cancel_frees_the_users_limit(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, [f"A{n}" for n in range(1, 9)], per_user_limit=4)
    first = (await reserve(client, show_id, "alice", ["A1", "A2", "A3", "A4"])).json()
    assert (await reserve(client, show_id, "alice", ["A5"])).status_code == 409

    await cancel(client, first["reservation_id"], "alice")

    assert (await reserve(client, show_id, "alice", ["A5", "A6", "A7", "A8"])).status_code == 201


async def test_only_the_owner_can_cancel(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    booked = (await reserve(client, show_id, "alice", ["A1"])).json()

    response = await cancel(client, booked["reservation_id"], "mallory")

    assert response.status_code == 404  # no hint that the reservation exists
    assert await seat_status(client, show_id) == {"A1": "confirmed"}


async def test_cancelling_twice_is_409(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    booked = (await reserve(client, show_id, "alice", ["A1"])).json()
    await cancel(client, booked["reservation_id"], "alice")

    response = await cancel(client, booked["reservation_id"], "alice")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_cancelled"


async def test_unknown_reservation_is_404(client: httpx.AsyncClient) -> None:
    assert (await cancel(client, str(uuid.uuid4()), "alice")).status_code == 404


async def test_a_stale_cancel_never_frees_a_seat_resold_to_someone_else(
    client: httpx.AsyncClient,
) -> None:
    show_id = await create_show(client, ["A12"])
    alice = (await reserve(client, show_id, "alice", ["A12"])).json()
    await cancel(client, alice["reservation_id"], "alice")
    bob = await reserve(client, show_id, "bob", ["A12"])
    assert bob.status_code == 201

    again = await cancel(client, alice["reservation_id"], "alice")

    assert again.status_code == 409
    assert await seat_status(client, show_id) == {"A12": "confirmed"}
    owner = await client.get(
        f"/reservations/{bob.json()['reservation_id']}", headers=user_headers("bob")
    )
    assert owner.json()["status"] == "confirmed"


async def test_racing_cancels_one_wins(client: httpx.AsyncClient, db: asyncpg.Connection) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    booked = (await reserve(client, show_id, "alice", ["A1", "A2"])).json()

    responses = await asyncio.gather(
        *(cancel(client, booked["reservation_id"], "alice") for _ in range(10))
    )

    assert Counter(r.status_code for r in responses) == {200: 1, 409: 9}
    held = await db.fetchval(
        "SELECT seat_count FROM user_show_holdings WHERE user_id = 'alice' AND show_id = $1",
        uuid.UUID(show_id),
    )
    assert held == 0  # decremented once, not ten times


async def test_cancel_racing_rebooks_has_one_new_winner(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A12"])
    booked = (await reserve(client, show_id, "alice", ["A12"])).json()

    results = await asyncio.gather(
        cancel(client, booked["reservation_id"], "alice"),
        *(reserve(client, show_id, f"rebooker-{n}", ["A12"]) for n in range(30)),
    )

    assert results[0].status_code == 200
    rebooks = Counter(r.status_code for r in results[1:])
    assert rebooks[201] <= 1 and set(rebooks) <= {201, 409}
    assert (
        sum(1 for s in (await seat_status(client, show_id)).values() if s == "confirmed")
        == rebooks[201]
    )


async def test_replaying_the_original_key_after_cancel_shows_it_cancelled(
    client: httpx.AsyncClient,
) -> None:
    show_id = await create_show(client, ["A1"])
    key = f"order-{uuid.uuid4()}"
    booked = (await reserve(client, show_id, "alice", ["A1"], key=key)).json()
    await cancel(client, booked["reservation_id"], "alice")

    replay = await reserve(client, show_id, "alice", ["A1"], key=key)

    # The key stays bound to that reservation: a late retry must not book the seat again.
    assert replay.status_code == 200
    assert replay.json()["status"] == "cancelled"
    assert await seat_status(client, show_id) == {"A1": "available"}


async def test_get_reservation_is_owner_only(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    booked = (await reserve(client, show_id, "alice", ["A1"])).json()
    path = f"/reservations/{booked['reservation_id']}"

    mine = await client.get(path, headers=user_headers("alice"))
    theirs = await client.get(path, headers=user_headers("bob"))

    assert mine.status_code == 200 and mine.json() == booked
    assert theirs.status_code == 404
