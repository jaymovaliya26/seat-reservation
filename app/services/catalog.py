"""Per-worker cache of what never changes about a show: price, per-user limit, seat labels.

Shows are immutable after creation, so caching them needs no invalidation. The cache lets the
reserve path validate seats and price a booking without a database round trip.

At on-sale time, thousands of requests for a brand-new show arrive at once. Without care, each
would miss the cache and load the whole hall. Instead, the first request starts one load and
every other request awaits that same load ("single flight").
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class ShowInfo:
    id: UUID
    price_paise: int
    per_user_limit: int
    seat_labels: frozenset[str]


class ShowCatalog:
    def __init__(self, pool: asyncpg.Pool, max_shows: int = 256) -> None:
        self._pool = pool
        self._max_shows = max_shows
        self._loads: OrderedDict[UUID, asyncio.Task[ShowInfo | None]] = OrderedDict()

    async def get(self, show_id: UUID) -> ShowInfo | None:
        load = self._loads.get(show_id)
        if load is None:
            load = asyncio.create_task(self._load(show_id))
            self._loads[show_id] = load
            while len(self._loads) > self._max_shows:
                self._loads.popitem(last=False)
        else:
            self._loads.move_to_end(show_id)

        try:
            # shield: if this caller is cancelled, the shared load keeps going for the others.
            info = await asyncio.shield(load)
        except Exception:
            self._forget(show_id, load)  # let the next request retry a failed load
            raise
        if info is None:
            self._forget(show_id, load)  # the show may be created later; don't cache a miss
        return info

    def _forget(self, show_id: UUID, load: asyncio.Task[ShowInfo | None]) -> None:
        if self._loads.get(show_id) is load:
            del self._loads[show_id]

    async def _load(self, show_id: UUID) -> ShowInfo | None:
        async with self._pool.acquire() as conn:
            show = await conn.fetchrow(
                "SELECT price_paise, per_user_limit FROM shows WHERE id = $1", show_id
            )
            if show is None:
                return None
            labels = await conn.fetch("SELECT label FROM seats WHERE show_id = $1", show_id)
        return ShowInfo(
            id=show_id,
            price_paise=show["price_paise"],
            per_user_limit=show["per_user_limit"],
            seat_labels=frozenset(row["label"] for row in labels),
        )
