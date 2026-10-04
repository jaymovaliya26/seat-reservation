"""Reserving seats: the one place where "who gets this seat" is decided.

Every decision is made by Postgres inside one transaction. There is no "is it free? then take
it" in Python; that read-then-write would double-sell under load.

The transaction takes its locks in one fixed order, the same for every request:

    1. the idempotency key   (unique index on reservations (user_id, idempotency_key))
    2. the user's holdings   (user_show_holdings row for this user and show)
    3. the seats             (seats rows, in label order)

Because everyone acquires locks in that order, no two transactions can each hold something the
other is waiting for, so they cannot deadlock.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass

import asyncpg
import structlog

from app.db import with_retries
from app.errors import (
    AlreadyCancelled,
    IdempotencyKeyReused,
    NotFound,
    PerUserLimit,
    SeatTaken,
)
from app.models import ReservationOut
from app.services.catalog import ShowInfo

log = structlog.get_logger(component="reservations")

_FIND_BY_KEY = """
SELECT id, show_id, user_id, seats, amount_paise, status, created_at, request_hash
  FROM reservations
 WHERE user_id = $1 AND idempotency_key = $2
"""

# Step 1. If a request with this key is in flight, this INSERT waits for it on the unique index.
# If that request commits, the conflict makes this a no-op (no row returned) and we replay it.
_INSERT_RESERVATION = """
INSERT INTO reservations
       (id, show_id, user_id, seats, amount_paise, status, idempotency_key, request_hash)
VALUES ($1, $2, $3, $4, $5, 'confirmed', $6, $7)
ON CONFLICT (user_id, idempotency_key) DO NOTHING
RETURNING created_at
"""

# Step 2. Add the seats to the user's count only if the total stays within the limit. The upsert
# locks the user's row, so the same user's parallel requests take turns here.
_ADD_TO_HOLDINGS = """
INSERT INTO user_show_holdings AS h (user_id, show_id, seat_count)
VALUES ($1, $2, $3)
ON CONFLICT (user_id, show_id) DO UPDATE
   SET seat_count = h.seat_count + EXCLUDED.seat_count
 WHERE h.seat_count + EXCLUDED.seat_count <= $4
RETURNING seat_count
"""

# Step 3. Lock every requested seat that is still available, in label order, then confirm them.
#
# Race-free: a request blocked on a seat's row lock re-checks `status = 'available'` against
# the newly committed row once the lock is released (READ COMMITTED's re-check of updated rows).
# If the seat was just sold, it drops out and this request claims fewer seats than it asked for.
#
# We wait instead of SKIP LOCKED: if the first claimer rolls back, the waiter still gets the
# seat. With SKIP LOCKED both could walk away and the seat would end up with no winner.
_CLAIM_SEATS = """
WITH claim AS (
    SELECT show_id, label
      FROM seats
     WHERE show_id = $1
       AND label = ANY($2::text[])
       AND status = 'available'
     ORDER BY label
       FOR UPDATE
)
UPDATE seats AS s
   SET status = 'confirmed', reservation_id = $3, user_id = $4, updated_at = now()
  FROM claim
 WHERE s.show_id = claim.show_id
   AND s.label = claim.label
RETURNING s.label
"""


@dataclass(frozen=True, slots=True)
class ReserveResult:
    reservation: ReservationOut
    replayed: bool


def request_fingerprint(show_id: uuid.UUID, seats: list[str]) -> bytes:
    """What the request asked for, so a reused key with a different body can be refused.

    Seats are sorted, so ["A13", "A12"] and ["A12", "A13"] count as the same request.
    """
    canonical = json.dumps({"show_id": str(show_id), "seats": sorted(seats)}, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()


async def reserve(
    pool: asyncpg.Pool,
    show: ShowInfo,
    user_id: str,
    seats: list[str],
    idempotency_key: str | None,
) -> ReserveResult:
    """Confirm all of `seats` for `user_id`, or none of them.

    Raises SeatTaken, PerUserLimit or IdempotencyKeyReused (all 409). A repeat of an earlier
    request with the same key returns that reservation with replayed=True and changes nothing.
    Without a key the request gets a fresh one: it works, but a retry of it would book again.
    """
    wanted = sorted(seats)
    fingerprint = request_fingerprint(show.id, wanted)

    if len(wanted) > show.per_user_limit:
        raise PerUserLimit(
            f"At most {show.per_user_limit} seats per user for this show",
            limit=show.per_user_limit,
            requested=len(wanted),
        )

    if idempotency_key is None:
        key = f"auto:{uuid.uuid4()}"
    else:
        key = idempotency_key
        # Retries are common (timeouts, impatient clients). Answer them from the stored
        # reservation without opening a write transaction. The transaction below still handles
        # retries that race with the original.
        existing = await pool.fetchrow(_FIND_BY_KEY, user_id, key)
        if existing is not None:
            return _replay(existing, fingerprint)

    return await with_retries(lambda: _reserve_once(pool, show, user_id, wanted, key, fingerprint))


async def _reserve_once(
    pool: asyncpg.Pool,
    show: ShowInfo,
    user_id: str,
    wanted: list[str],
    idempotency_key: str,
    fingerprint: bytes,
) -> ReserveResult:
    reservation_id = uuid.uuid4()
    amount_paise = show.price_paise * len(wanted)

    async with pool.acquire() as conn, conn.transaction():
        created_at = await conn.fetchval(
            _INSERT_RESERVATION,
            reservation_id,
            show.id,
            user_id,
            wanted,
            amount_paise,
            idempotency_key,
            fingerprint,
        )
        if created_at is None:
            existing = await conn.fetchrow(_FIND_BY_KEY, user_id, idempotency_key)
            return _replay(existing, fingerprint)

        held = await conn.fetchval(
            _ADD_TO_HOLDINGS, user_id, show.id, len(wanted), show.per_user_limit
        )
        if held is None:
            current = await conn.fetchval(
                "SELECT seat_count FROM user_show_holdings WHERE user_id = $1 AND show_id = $2",
                user_id,
                show.id,
            )
            raise PerUserLimit(
                f"At most {show.per_user_limit} seats per user for this show",
                limit=show.per_user_limit,
                held=current,
                requested=len(wanted),
            )

        claimed = {
            row["label"]
            for row in await conn.fetch(_CLAIM_SEATS, show.id, wanted, reservation_id, user_id)
        }
        if len(claimed) != len(wanted):
            # All-or-nothing: raising inside the transaction rolls back the reservation row,
            # the holdings increment, and every seat this request did manage to lock.
            taken = [seat for seat in wanted if seat not in claimed]
            raise SeatTaken("Seats already taken: " + ", ".join(taken), seats=taken)

    return ReserveResult(
        reservation=ReservationOut(
            reservation_id=reservation_id,
            show_id=show.id,
            user_id=user_id,
            seats=wanted,
            amount_paise=amount_paise,
            status="confirmed",
            created_at=created_at,
        ),
        replayed=False,
    )


def _replay(existing: asyncpg.Record, fingerprint: bytes) -> ReserveResult:
    if existing["request_hash"] != fingerprint:
        raise IdempotencyKeyReused(
            "This idempotency key was already used for a different request",
            reservation_id=str(existing["id"]),
        )
    return ReserveResult(reservation=_to_out(existing), replayed=True)


_SELECT_RESERVATION = """
SELECT id, show_id, user_id, seats, amount_paise, status, created_at
  FROM reservations
 WHERE id = $1 AND user_id = $2
"""

# Free exactly the seats this reservation holds, locking them in label order like a claim does.
# Matching on reservation_id is what makes a cancel safe: a seat that has since been sold to
# someone else carries their reservation_id, so it can never be freed by mistake.
_RELEASE_SEATS = """
WITH release AS (
    SELECT show_id, label
      FROM seats
     WHERE show_id = $1
       AND reservation_id = $2
     ORDER BY label
       FOR UPDATE
)
UPDATE seats AS s
   SET status = 'available', reservation_id = NULL, user_id = NULL, updated_at = now()
  FROM release
 WHERE s.show_id = release.show_id
   AND s.label = release.label
RETURNING s.label
"""


async def get_reservation(
    pool: asyncpg.Pool, reservation_id: uuid.UUID, user_id: str
) -> ReservationOut | None:
    """The reservation, if it exists and belongs to `user_id`. Otherwise None, so callers
    answer 404 and never reveal that someone else's reservation exists."""
    row = await pool.fetchrow(_SELECT_RESERVATION, reservation_id, user_id)
    return None if row is None else _to_out(row)


async def cancel(pool: asyncpg.Pool, reservation_id: uuid.UUID, user_id: str) -> ReservationOut:
    """Cancel the caller's own reservation and return its seats to sale.

    Raises NotFound (missing or not yours) or AlreadyCancelled (409).
    """
    return await with_retries(lambda: _cancel_once(pool, reservation_id, user_id))


async def _cancel_once(
    pool: asyncpg.Pool, reservation_id: uuid.UUID, user_id: str
) -> ReservationOut:
    async with pool.acquire() as conn, conn.transaction():
        # Locking the reservation row makes two racing cancels take turns; the second sees
        # 'cancelled' once the first commits.
        row = await conn.fetchrow(_SELECT_RESERVATION + " FOR UPDATE", reservation_id, user_id)
        if row is None:
            raise NotFound("Reservation not found", reservation_id=str(reservation_id))
        if row["status"] == "cancelled":
            raise AlreadyCancelled(
                "Reservation is already cancelled", reservation_id=str(reservation_id)
            )

        # Same lock order as a reservation: holdings first, then seats.
        await conn.execute(
            "UPDATE user_show_holdings SET seat_count = seat_count - $3"
            " WHERE user_id = $1 AND show_id = $2",
            user_id,
            row["show_id"],
            len(row["seats"]),
        )
        freed = await conn.fetch(_RELEASE_SEATS, row["show_id"], reservation_id)
        if len(freed) != len(row["seats"]):
            # Cannot happen while the schema constraints hold. If it does, refuse to
            # half-cancel: raising rolls everything back, and reconcile will show why.
            raise RuntimeError(
                f"reservation {reservation_id} lists {len(row['seats'])} seats "
                f"but owns {len(freed)}"
            )
        cancelled_at = await conn.fetchval(
            "UPDATE reservations SET status = 'cancelled', cancelled_at = now()"
            " WHERE id = $1 RETURNING cancelled_at",
            reservation_id,
        )
    log.info(
        "reservation_cancelled", reservation_id=str(reservation_id), cancelled_at=str(cancelled_at)
    )
    return _to_out(row).model_copy(update={"status": "cancelled"})


def _to_out(row: asyncpg.Record) -> ReservationOut:
    return ReservationOut(
        reservation_id=row["id"],
        show_id=row["show_id"],
        user_id=row["user_id"],
        seats=list(row["seats"]),
        amount_paise=row["amount_paise"],
        status=row["status"],
        created_at=row["created_at"],
    )
