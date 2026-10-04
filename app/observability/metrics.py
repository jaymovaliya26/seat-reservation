"""Prometheus metrics.

Gunicorn runs several worker processes and a scrape reaches only one of them. In production
PROMETHEUS_MULTIPROC_DIR is set, so every worker writes its counters to shared files and
/metrics adds them up: the numbers describe the whole service, whichever worker answers.

Seat gauges are not kept in memory at all. They are read from Postgres at scrape time with the
same counting as GET /shows/{id}, so the metrics can never drift from the API.
"""

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass

import asyncpg
import structlog
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = structlog.get_logger(component="metrics")

MULTIPROCESS = "PROMETHEUS_MULTIPROC_DIR" in os.environ
CONTENT_TYPE = CONTENT_TYPE_LATEST

# How many of the most recent shows get seat gauges. Bounds label cardinality and scrape cost.
SEAT_GAUGE_SHOWS = 20

DECLINE_REASONS = (
    "seat_taken",
    "per_user_limit",
    "idempotent_replay",
    "idempotency_key_reused",
    "unknown_seats",
    "not_found",
    "invalid_request",
)

RESERVATIONS_CONFIRMED = Counter("reservations_confirmed", "Reservations created (201)")
RESERVATIONS_DECLINED = Counter(
    "reservations_declined",
    "Reserve requests that did not create a reservation, by reason",
    ["reason"],
)
RESERVATIONS_CANCELLED = Counter("reservations_cancelled", "Reservations cancelled")
SEATS_SOLD = Counter("seats_sold", "Seats confirmed by new reservations")
SEATS_RELEASED = Counter("seats_released", "Seats returned to sale by cancellations")

HTTP_REQUESTS = Counter(
    "http_requests", "HTTP requests by route template and status", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

DB_POOL_CONNECTIONS = Gauge(
    "db_pool_connections",
    "Database connections across all live workers",
    ["state"],
    multiprocess_mode="livesum",
)
DB_TX_RETRIES = Counter(
    "db_transaction_retries",
    "Transactions retried after a deadlock or serialization failure",
    ["sqlstate"],
)
RESERVE_WITHOUT_KEY = Counter(
    "reserve_requests_without_idempotency_key", "Reserve requests that sent no idempotency key"
)
SPOOFED_USER_ID = Counter(
    "spoofed_user_id", "Requests whose body named a user other than the token's (ignored)"
)
LOG_LINES_SUPPRESSED = Counter(
    "log_lines_suppressed",
    "Request log lines skipped by the per-worker cap (every request is still in http_requests)",
)
APP_INFO = Gauge("app_info", "Running version", ["version"], multiprocess_mode="max")

# Pre-create every reason so a decline counter reads 0, not "missing", before its first use.
for _reason in DECLINE_REASONS:
    RESERVATIONS_DECLINED.labels(reason=_reason)


@dataclass(frozen=True, slots=True)
class ShowSeats:
    show_id: str
    total: int
    available: int
    held: int
    confirmed: int


_SEAT_COUNTS = f"""
SELECT s.id::text AS show_id,
       s.total_seats AS total,
       count(*) FILTER (WHERE st.status = 'available') AS available,
       count(*) FILTER (WHERE st.status = 'held')      AS held,
       count(*) FILTER (WHERE st.status = 'confirmed') AS confirmed
  FROM (SELECT id, total_seats FROM shows ORDER BY created_at DESC LIMIT {SEAT_GAUGE_SHOWS}) s
  JOIN seats st ON st.show_id = s.id
 GROUP BY s.id, s.total_seats
"""


class _SeatCollector(Collector):
    """Exposes a seat snapshot taken just before the scrape."""

    def __init__(self, shows: list[ShowSeats]) -> None:
        self._shows = shows

    def collect(self) -> Iterator[GaugeMetricFamily]:
        families = {
            "available": GaugeMetricFamily("seats_available", "Seats on sale", labels=["show_id"]),
            "held": GaugeMetricFamily("seats_held", "Seats on hold", labels=["show_id"]),
            "confirmed": GaugeMetricFamily("seats_confirmed", "Seats sold", labels=["show_id"]),
            "total": GaugeMetricFamily("seats_total", "Seats in the hall", labels=["show_id"]),
        }
        invariant = GaugeMetricFamily(
            "seats_invariant_ok",
            "1 if available + held + confirmed == total for the show, else 0",
            labels=["show_id"],
        )
        for show in self._shows:
            for field, family in families.items():
                family.add_metric([show.show_id], getattr(show, field))
            balanced = show.available + show.held + show.confirmed == show.total
            invariant.add_metric([show.show_id], 1 if balanced else 0)
        yield from families.values()
        yield invariant


async def render(pool: asyncpg.Pool, version: str) -> bytes:
    APP_INFO.labels(version=version).set(1)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SEAT_COUNTS)
    seats = _SeatCollector([ShowSeats(**dict(row)) for row in rows])

    per_scrape = CollectorRegistry()
    per_scrape.register(seats)
    if MULTIPROCESS:
        multiprocess.MultiProcessCollector(per_scrape)  # type: ignore[no-untyped-call]
        return generate_latest(per_scrape)
    return generate_latest(REGISTRY) + generate_latest(per_scrape)


async def sample_pool(pool: asyncpg.Pool, interval_s: float = 5.0) -> None:
    """Keep this worker's share of the pool gauges current, until cancelled."""
    while True:
        size, idle = pool.get_size(), pool.get_idle_size()
        DB_POOL_CONNECTIONS.labels(state="in_use").set(size - idle)
        DB_POOL_CONNECTIONS.labels(state="idle").set(idle)
        DB_POOL_CONNECTIONS.labels(state="max").set(pool.get_max_size())
        await asyncio.sleep(interval_s)
