# Observability

How to see that the service is behaving correctly while a burst is running, and what should wake someone up.

## Watch a burst live

```bash
BASE=https://seat-reservation-jm.up.railway.app      # or http://localhost:8000

# Outcomes, declines by reason, and seat gauges, refreshed every second
watch -n1 "curl -s $BASE/metrics | grep -E '^(reservations_|seats_(available|confirmed|invariant_ok)|db_pool)'"

# Live logs from Railway (one JSON line per request)
railway logs --service app

# Full audit of one show's books
curl -s $BASE/admin/shows/<show_id>/reconcile -H "X-Admin-Key: $ADMIN_KEY" | jq
```

Locally you can also run Prometheus with the alert rules loaded:

```bash
docker compose --profile observability up -d     # Prometheus on http://localhost:9090
```

## Metrics (`GET /metrics`)

Gunicorn runs 4 worker processes, and a scrape reaches only one of them. Prometheus's multiprocess mode makes each worker write to shared files, and `/metrics` adds them up, so the numbers describe the whole service. Seat gauges are not held in memory: they are read from Postgres at scrape time with the same counting as `GET /shows/{id}`, so they cannot drift from the API.

| Metric | Type | Meaning |
|---|---|---|
| `reservations_confirmed_total` | counter | Reservations created (each `201`) |
| `reservations_declined_total{reason}` | counter | Reserve requests that booked nothing: `seat_taken`, `per_user_limit`, `idempotent_replay`, `idempotency_key_reused`, `unknown_seats`, `not_found`, `invalid_request` |
| `reservations_cancelled_total` | counter | Successful cancellations |
| `seats_sold_total`, `seats_released_total` | counter | Seats confirmed by reservations / returned by cancellations |
| `seats_available{show_id}`, `seats_held{show_id}`, `seats_confirmed{show_id}`, `seats_total{show_id}` | gauge | Live seat state for the 20 most recent shows, from Postgres |
| `seats_invariant_ok{show_id}` | gauge | `1` if available + held + confirmed == total, else `0` |
| `http_requests_total{method,route,status}` | counter | Every request, labelled by route template (never by raw ID) |
| `http_request_duration_seconds{method,route}` | histogram | Latency, for p50/p99 |
| `db_pool_connections{state}` | gauge | `in_use`, `idle` and `max` connections summed over live workers (sampled every 5s) |
| `db_transaction_retries_total{sqlstate}` | counter | Transactions rerun after a deadlock (`40P01`) or serialization failure (`40001`) |
| `reserve_requests_without_idempotency_key_total` | counter | Reserve requests that weren't retry-safe |
| `spoofed_user_id_total` | counter | Requests whose body named another user (ignored) |
| `log_lines_suppressed_total` | counter | Request log lines skipped by the log budget |
| `app_info{version}` | gauge | The running version |

**How the numbers reconcile.** For any window of reserve traffic:
- the count of `201` responses equals the increase in `reservations_confirmed_total`;
- each `409` equals the increase in `reservations_declined_total` for its `error.code`;
- each `200` replay equals the increase in `reservations_declined_total{reason="idempotent_replay"}`.

`scripts/smoke.sh` checks exactly this on every CI run, against the 4-worker container.

Body validation failures (malformed JSON, a float price) are rejected before the reserve logic runs, so they show up only as `http_requests_total{status="422"}`.

Counters start from zero when the service restarts, as Prometheus counters do; Prometheus's `rate()` and `increase()` handle the reset.

## Logs

One JSON object per line on stdout:

```json
{"component":"http","method":"POST","path":"/shows/…/reserve","route":"/shows/{show_id}/reserve","status":409,"duration_ms":4.21,"request_id":"9f2c…","user_id":"alice","show_id":"…","error_code":"seat_taken","level":"info","ts":"2026-10-04T13:40:01.123Z","msg":"request"}
```

- **`request_id`** comes from the caller's `X-Request-ID` header if valid, otherwise it is generated. It is returned on every response and in every error body, so a client's complaint can be traced to one log line.
- **Request context.** `user_id`, `show_id`, `outcome` (`confirmed`, `replayed`, `cancelled`), `reservation_id` and `error_code` are attached to the request's line.
- **Unhandled errors** log `unhandled_error` with the stack trace and the same `request_id`.
- **Everything else is JSON too.** Gunicorn, Uvicorn and asyncpg lines use the same shape, with a `component` field.
- **Log budget.** Railway keeps at most 500 lines per second per replica, and in practice about 250 per second per deployment, and silently drops the rest. Each worker therefore writes at most 25 request lines per second, counted in whole wall-clock seconds (`LOG_REQUEST_LINES_PER_SECOND`), and 5xx lines are always written. Skipped lines become one `request_lines_suppressed` summary per second with counts by status, and are counted in `log_lines_suppressed_total`. Every request is still counted in `/metrics`, and every booking is a row in Postgres.

## Health

| Probe | Meaning | Used by |
|---|---|---|
| `GET /healthz` | The process is serving. Never touches the database. | Docker `HEALTHCHECK` (restart if stuck) |
| `GET /readyz` | Postgres answered `SELECT 1` within 1s, using a separate two-connection pool so a busy main pool can't fake an outage. `503` otherwise. | Railway: traffic only goes to an instance once this is `200` |

## What pages at 2am

Rules live in [`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml) and are validated with `promtool`.

| Alert | Fires when | Why it matters |
|---|---|---|
| **SeatInvariantBroken** (page, immediately) | `seats_invariant_ok == 0` | Seats may be double-sold or lost. The only alert with no grace period. Stop sales for the show and run reconcile. |
| **ServerErrors** (page) | 5xx above 0.5% of requests for 5 min | Declines are 4xx by design, so a steady 5xx rate is a real failure. |
| **ServiceDown** (page) | Scrape failing for 2 min | Nobody can buy. |
| **ReserveLatencyHigh** (page) | Reserve p99 above 1s for 10 min | Buyers are waiting; usually database saturation. |
| **DatabasePoolSaturated** (ticket) | Over 90% of connections busy for 5 min | Capacity warning before it becomes latency. |
| **TransactionRetriesRising** (ticket) | More than 1 retry/s for 5 min | The lock order should prevent deadlocks, so a sustained rate means some code path broke it. |

Things that are deliberately **not** alerts: high `seat_taken` or `per_user_limit` counts. During an on-sale they are the system working.
