"""Reserving seats: the one place where "who gets this seat" is decided.

The decision is a single SQL statement that Postgres executes atomically. There is no
"is it free? then take it" in Python; that read-then-write would double-sell under load.
"""

import uuid

import asyncpg

from app.errors import SeatTaken
from app.models import ReservationOut
from app.services.catalog import ShowInfo

# Lock every requested seat that is still available, in label order, then confirm them.
#
# Why this is race-free: when two requests want the same seat, the second blocks on the row
# lock. Once the first commits, Postgres re-checks `status = 'available'` against the newly
# committed row (READ COMMITTED's re-check of updated rows). The seat is now confirmed, so it
# drops out and the second request claims fewer seats than it asked for.
#
# Why it can't deadlock: every transaction locks seats in the same order (by label), so two
# overlapping multi-seat requests can never each hold a seat the other is waiting for.
#
# Why we wait instead of SKIP LOCKED: if the first claimer rolls back, the waiter still gets
# the seat. With SKIP LOCKED both could walk away and the seat would end up with no winner.
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


async def reserve(
    pool: asyncpg.Pool, show: ShowInfo, user_id: str, seats: list[str]
) -> ReservationOut:
    """Confirm all of `seats` for `user_id`, or none of them (raises SeatTaken)."""
    wanted = sorted(seats)
    reservation_id = uuid.uuid4()
    amount_paise = show.price_paise * len(wanted)

    async with pool.acquire() as conn, conn.transaction():
        created_at = await conn.fetchval(
            """
            INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)
            VALUES ($1, $2, $3, $4, $5, 'confirmed')
            RETURNING created_at
            """,
            reservation_id,
            show.id,
            user_id,
            wanted,
            amount_paise,
        )
        claimed = {
            row["label"]
            for row in await conn.fetch(_CLAIM_SEATS, show.id, wanted, reservation_id, user_id)
        }
        if len(claimed) != len(wanted):
            # All-or-nothing: raising inside the transaction rolls back the reservation row
            # and every seat this request did manage to lock.
            taken = [seat for seat in wanted if seat not in claimed]
            raise SeatTaken("Seats already taken: " + ", ".join(taken), seats=taken)

    return ReservationOut(
        reservation_id=reservation_id,
        show_id=show.id,
        user_id=user_id,
        seats=wanted,
        amount_paise=amount_paise,
        status="confirmed",
        created_at=created_at,
    )
