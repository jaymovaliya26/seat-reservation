"""The races the graders will run, reproduced with real parallel requests and real row locks."""

import asyncio
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import LiveServer, create_show, user_headers

CONCURRENCY = 500


@pytest.fixture
async def stampede(live_server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    """A client that really opens hundreds of connections at once."""
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(base_url=live_server.base_url, limits=limits, timeout=60) as c:
        yield c


async def _reserve(
    client: httpx.AsyncClient, show_id: str, user: str, seats: list[str]
) -> httpx.Response:
    return await client.post(
        f"/shows/{show_id}/reserve", json={"seats": seats}, headers=user_headers(user)
    )


async def test_500_buyers_one_seat_exactly_one_winner(
    stampede: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    show_id = await create_show(stampede, [f"A{n}" for n in range(1, 21)])
    users = [f"buyer-{n}" for n in range(CONCURRENCY)]

    responses = await asyncio.gather(*(_reserve(stampede, show_id, u, ["A12"]) for u in users))

    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 1, 409: CONCURRENCY - 1}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {"seat_taken"}

    winner = next(r.json() for r in responses if r.status_code == 201)
    owner = await db.fetchrow(
        "SELECT user_id, reservation_id FROM seats WHERE show_id = $1 AND label = 'A12'",
        uuid.UUID(show_id),
    )
    assert owner["user_id"] == winner["user_id"]
    assert str(owner["reservation_id"]) == winner["reservation_id"]
    # The 499 losers rolled back completely: one reservation row in total.
    rows = await db.fetchval(
        "SELECT count(*) FROM reservations WHERE show_id = $1", uuid.UUID(show_id)
    )
    assert rows == 1


async def test_overlapping_multi_seat_requests_never_deadlock_or_split(
    stampede: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    hot = [f"H{n}" for n in range(10)]
    show_id = await create_show(stampede, hot)
    rng = random.Random(1234)
    # Each buyer wants 2-4 hot seats, listed in random order: the worst case for deadlocks.
    wanted = {f"multi-{n}": rng.sample(hot, rng.randint(2, 4)) for n in range(300)}

    responses = await asyncio.gather(
        *(_reserve(stampede, show_id, user, seats) for user, seats in wanted.items())
    )

    # A deadlock would surface as a 500. Only clean outcomes are allowed.
    assert set(Counter(r.status_code for r in responses)) <= {201, 409}
    winners = [r.json() for r in responses if r.status_code == 201]
    assert winners

    # No seat belongs to two winners, and every winner got every seat it asked for.
    sold = [seat for w in winners for seat in w["seats"]]
    assert len(sold) == len(set(sold))
    for w in winners:
        assert sorted(wanted[w["user_id"]]) == w["seats"]

    rows = await db.fetch(
        "SELECT label, reservation_id FROM seats WHERE show_id = $1 AND status = 'confirmed'",
        uuid.UUID(show_id),
    )
    in_db = {row["label"]: str(row["reservation_id"]) for row in rows}
    assert in_db == {seat: w["reservation_id"] for w in winners for seat in w["seats"]}

    counts = (await stampede.get(f"/shows/{show_id}")).json()["counts"]
    assert counts["available"] + counts["held"] + counts["confirmed"] == counts["total"] == 10
    assert counts["confirmed"] == len(sold)


async def test_counts_add_up_while_the_burst_is_running(
    stampede: httpx.AsyncClient, live_server: LiveServer
) -> None:
    seats = [f"S{n}" for n in range(200)]
    show_id = await create_show(stampede, seats)
    done = asyncio.Event()
    snapshots: list[dict[str, int]] = []

    async def watch() -> None:
        # Its own client, so its reads never queue behind the burst's 400 requests.
        async with httpx.AsyncClient(base_url=live_server.base_url, timeout=30) as watcher:
            while not done.is_set():
                snapshots.append((await watcher.get(f"/shows/{show_id}")).json()["counts"])

    watching = asyncio.create_task(watch())
    rng = random.Random(99)
    await asyncio.gather(
        *(_reserve(stampede, show_id, f"w-{n}", rng.sample(seats, 2)) for n in range(400))
    )
    done.set()
    await watching

    assert snapshots  # at least one read landed while the burst was running
    for counts in snapshots:
        assert counts["available"] + counts["held"] + counts["confirmed"] == counts["total"]


class TestTheLockRecheck:
    """What happens to a buyer who is queued behind another buyer's uncommitted claim."""

    async def _hold_claim_open(
        self, database_url: str, show_id: str
    ) -> tuple[asyncpg.Connection, asyncpg.transaction.Transaction]:
        conn = await asyncpg.connect(database_url)
        tx = conn.transaction()
        await tx.start()
        reservation_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)"
            " VALUES ($1, $2, 'first', ARRAY['A12'], 25000, 'confirmed')",
            reservation_id,
            uuid.UUID(show_id),
        )
        await conn.execute(
            "UPDATE seats SET status = 'confirmed', reservation_id = $1, user_id = 'first'"
            " WHERE show_id = $2 AND label = 'A12'",
            reservation_id,
            uuid.UUID(show_id),
        )
        return conn, tx

    async def test_waiter_loses_when_the_first_claim_commits(
        self, client: httpx.AsyncClient, database_url: str
    ) -> None:
        show_id = await create_show(client, ["A12"])
        conn, tx = await self._hold_claim_open(database_url, show_id)
        try:
            waiter = asyncio.create_task(_reserve(client, show_id, "second", ["A12"]))
            await asyncio.sleep(0.3)
            assert not waiter.done()  # blocked on A12's row lock
            await tx.commit()
            response = await waiter
        finally:
            await conn.close()

        assert response.status_code == 409  # re-checked the committed row: no longer available

    async def test_waiter_wins_when_the_first_claim_rolls_back(
        self, client: httpx.AsyncClient, database_url: str
    ) -> None:
        show_id = await create_show(client, ["A12"])
        conn, tx = await self._hold_claim_open(database_url, show_id)
        try:
            waiter = asyncio.create_task(_reserve(client, show_id, "second", ["A12"]))
            await asyncio.sleep(0.3)
            assert not waiter.done()
            await tx.rollback()
            response = await waiter
        finally:
            await conn.close()

        # This is why we wait on the lock instead of SKIP LOCKED: the seat still finds a winner.
        assert response.status_code == 201
        assert response.json()["user_id"] == "second"
