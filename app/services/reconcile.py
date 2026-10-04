"""Audit one show's books: do seats, reservations and holdings all tell the same story?

Every check runs inside one REPEATABLE READ, read-only transaction, so all of them see the
same instant even while a burst is still writing. A failing check is the thing to page on: it
would mean money or seats are wrong, not just slow.
"""

import uuid

import asyncpg

from app.models import ReconcileCheck, ReconcileOut, SeatCounts

_MAX_EXAMPLES = 5

_COUNTS = """
SELECT s.total_seats,
       count(st.label)                                     AS seat_rows,
       count(*) FILTER (WHERE st.status = 'available')     AS available,
       count(*) FILTER (WHERE st.status = 'held')          AS held,
       count(*) FILTER (WHERE st.status = 'confirmed')     AS confirmed
  FROM shows s
  LEFT JOIN seats st ON st.show_id = s.id
 WHERE s.id = $1
 GROUP BY s.total_seats
"""

# Each query returns one text row per violation.
_CHECKS: dict[str, str] = {
    # The headline guarantee, checked from the reservations' side: no seat label appears in two
    # confirmed reservations. (The seats table cannot store it at all: one row per seat.)
    "no_seat_sold_twice": """
        SELECT seat || ' in ' || count(*) || ' reservations'
          FROM reservations r
         CROSS JOIN LATERAL unnest(r.seats) AS seat
         WHERE r.show_id = $1 AND r.status = 'confirmed'
         GROUP BY seat
        HAVING count(*) > 1
    """,
    # Every sold seat points at a confirmed reservation of the same user that lists it.
    "seats_match_reservations": """
        SELECT st.label || ' -> ' || coalesce(r.id::text, 'missing reservation')
          FROM seats st
          LEFT JOIN reservations r ON r.id = st.reservation_id
         WHERE st.show_id = $1
           AND st.status <> 'available'
           AND (r.id IS NULL
                OR r.status <> 'confirmed'
                OR r.user_id <> st.user_id
                OR NOT (st.label = ANY (r.seats)))
    """,
    # Every confirmed reservation owns all of its seats: nothing half-booked.
    "reservations_fully_seated": """
        SELECT r.id::text || ' lists ' || cardinality(r.seats) || ', owns ' || coalesce(c.n, 0)
          FROM reservations r
          LEFT JOIN (SELECT reservation_id, count(*) AS n
                       FROM seats
                      WHERE show_id = $1 AND status <> 'available'
                      GROUP BY reservation_id) c ON c.reservation_id = r.id
         WHERE r.show_id = $1
           AND r.status = 'confirmed'
           AND coalesce(c.n, 0) <> cardinality(r.seats)
    """,
    # The per-user counters agree with the seats each user actually owns.
    "holdings_match_seats": """
        WITH owned AS (
            SELECT user_id, count(*) AS n
              FROM seats
             WHERE show_id = $1 AND status <> 'available'
             GROUP BY user_id
        ), held AS (
            SELECT user_id, seat_count FROM user_show_holdings WHERE show_id = $1
        )
        SELECT coalesce(h.user_id, o.user_id)
               || ': counter ' || coalesce(h.seat_count, 0) || ', owns ' || coalesce(o.n, 0)
          FROM held h
          FULL JOIN owned o ON o.user_id = h.user_id
         WHERE coalesce(h.seat_count, 0) <> coalesce(o.n, 0)
    """,
    "within_user_limit": """
        SELECT h.user_id || ': ' || h.seat_count || ' > ' || s.per_user_limit
          FROM user_show_holdings h
          JOIN shows s ON s.id = h.show_id
         WHERE h.show_id = $1 AND h.seat_count > s.per_user_limit
    """,
    # Money is exact: price x seats, in integer paise.
    "amounts_are_exact": """
        SELECT r.id::text || ': ' || r.amount_paise
               || ' <> ' || s.price_paise * cardinality(r.seats)
          FROM reservations r
          JOIN shows s ON s.id = r.show_id
         WHERE r.show_id = $1 AND r.amount_paise <> s.price_paise * cardinality(r.seats)
    """,
}


async def reconcile(pool: asyncpg.Pool, show_id: uuid.UUID) -> ReconcileOut | None:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        counts = await conn.fetchrow(_COUNTS, show_id)
        if counts is None:
            return None

        checks: dict[str, ReconcileCheck] = {}
        total = counts["available"] + counts["held"] + counts["confirmed"]
        mismatch = total != counts["total_seats"] or counts["seat_rows"] != counts["total_seats"]
        checks["counts_add_up"] = ReconcileCheck(
            ok=not mismatch,
            violations=int(mismatch),
            examples=[
                f"available {counts['available']} + held {counts['held']} + confirmed "
                f"{counts['confirmed']} = {total}, total_seats {counts['total_seats']}"
            ]
            if mismatch
            else [],
        )
        for name, sql in _CHECKS.items():
            rows = await conn.fetch(sql, show_id)
            checks[name] = ReconcileCheck(
                ok=not rows,
                violations=len(rows),
                examples=[row[0] for row in rows[:_MAX_EXAMPLES]],
            )

    return ReconcileOut(
        show_id=show_id,
        ok=all(check.ok for check in checks.values()),
        counts=SeatCounts(
            total=counts["total_seats"],
            available=counts["available"],
            held=counts["held"],
            confirmed=counts["confirmed"],
        ),
        checks=checks,
    )
