import asyncio
import random
import uuid
from collections import Counter

import asyncpg
import httpx
import pytest

from tests.conftest import ADMIN_HEADERS, create_show, user_headers


async def reconcile(client: httpx.AsyncClient, show_id: str) -> dict:  # type: ignore[type-arg]
    response = await client.get(f"/admin/shows/{show_id}/reconcile", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


async def test_a_fresh_show_reconciles(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    report = await reconcile(client, show_id)
    assert report["ok"] is True
    assert report["counts"] == {"total": 2, "available": 2, "held": 0, "confirmed": 0}
    assert set(report["checks"]) == {
        "counts_add_up",
        "no_seat_sold_twice",
        "seats_match_reservations",
        "reservations_fully_seated",
        "holdings_match_seats",
        "within_user_limit",
        "amounts_are_exact",
    }


async def test_reconcile_requires_the_admin_key(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    response = await client.get(f"/admin/shows/{show_id}/reconcile")
    assert response.status_code == 401


async def test_reconcile_unknown_show_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/admin/shows/{uuid.uuid4()}/reconcile", headers=ADMIN_HEADERS)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("tamper", "failing_check"),
    [
        (
            "UPDATE user_show_holdings SET seat_count = seat_count + 1"
            " WHERE show_id = $1 AND user_id = 'alice'",
            "holdings_match_seats",
        ),
        (
            "UPDATE seats SET user_id = 'mallory' WHERE show_id = $1 AND label = 'A1'",
            "seats_match_reservations",
        ),
        (
            "UPDATE reservations SET amount_paise = amount_paise - 1 WHERE show_id = $1",
            "amounts_are_exact",
        ),
        (
            "UPDATE reservations SET seats = array_append(seats, 'A3') WHERE show_id = $1",
            "reservations_fully_seated",
        ),
    ],
)
async def test_reconcile_catches_corruption(
    client: httpx.AsyncClient, db: asyncpg.Connection, tamper: str, failing_check: str
) -> None:
    """Corrupt the books behind the API's back and check that the audit notices."""
    show_id = await create_show(client, ["A1", "A2", "A3"])
    await client.post(
        f"/shows/{show_id}/reserve", json={"seats": ["A1", "A2"]}, headers=user_headers("alice")
    )
    await db.execute(tamper, uuid.UUID(show_id))

    report = await reconcile(client, show_id)

    assert report["ok"] is False
    assert report["checks"][failing_check]["ok"] is False
    assert report["checks"][failing_check]["violations"] >= 1
    assert report["checks"][failing_check]["examples"]


async def test_books_balance_after_a_random_storm_of_reserves_cancels_and_retries(
    client: httpx.AsyncClient,
) -> None:
    """Thousands of random operations, run concurrently in waves, then a full audit."""
    seats = [f"R{n}" for n in range(40)]
    show_id = await create_show(client, seats, per_user_limit=4)
    users = [f"rnd-{n}" for n in range(25)]
    rng = random.Random(2026)
    booked: list[tuple[str, str]] = []  # (user, reservation_id)
    sent_keys: list[tuple[str, str, list[str]]] = []  # (user, key, seats)
    statuses: Counter[int] = Counter()

    async def reserve(user: str, wanted: list[str], key: str) -> None:
        response = await client.post(
            f"/shows/{show_id}/reserve",
            json={"seats": wanted, "idempotency_key": key},
            headers=user_headers(user),
        )
        statuses[response.status_code] += 1
        if response.status_code == 201:
            booked.append((user, response.json()["reservation_id"]))

    async def cancel(user: str, reservation_id: str) -> None:
        response = await client.post(
            f"/reservations/{reservation_id}/cancel", headers=user_headers(user)
        )
        statuses[response.status_code] += 1

    for _wave in range(6):
        ops = []
        for _ in range(120):
            roll = rng.random()
            if roll < 0.55 or not booked:
                user = rng.choice(users)
                wanted = rng.sample(seats, rng.randint(1, 3))
                key = f"{user}-{uuid.uuid4().hex[:8]}"
                sent_keys.append((user, key, wanted))
                ops.append(reserve(user, wanted, key))
            elif roll < 0.75:
                user, key, wanted = rng.choice(sent_keys)  # a retry, sometimes racing the original
                ops.append(reserve(user, wanted, key))
            else:
                user, reservation_id = rng.choice(booked)  # may already be cancelled
                ops.append(cancel(user, reservation_id))
        await asyncio.gather(*ops)

    assert set(statuses) <= {200, 201, 409}, statuses  # no 5xx, no surprises
    # The storm really exercised every path: bookings, replays or cancels (200), and declines.
    assert statuses[201] > 50 and statuses[200] > 50 and statuses[409] > 50, statuses
    report = await reconcile(client, show_id)
    assert report["ok"] is True, report
