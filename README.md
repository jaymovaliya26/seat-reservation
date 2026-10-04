# Seat Reservation

[![CI](https://github.com/jaymovaliya26/seat-reservation/actions/workflows/ci.yml/badge.svg)](https://github.com/jaymovaliya26/seat-reservation/actions/workflows/ci.yml)

A JSON API that sells assigned seats for a show and guarantees each seat is sold exactly once, even when thousands of buyers try to book the same seat in the same second.

## For reviewers

| | |
|---|---|
| **Live URL** | https://seat-reservation-jm.up.railway.app (Railway, Singapore) |
| **One-command burst** | `./scripts/burst.sh https://seat-reservation-jm.up.railway.app <ADMIN_KEY>` |
| **Metrics** | https://seat-reservation-jm.up.railway.app/metrics |
| **Health** | [`/healthz`](https://seat-reservation-jm.up.railway.app/healthz) (process alive), [`/readyz`](https://seat-reservation-jm.up.railway.app/readyz) (Postgres reachable, else 503) |
| **Logs** | JSON on stdout with `request_id`; a recording of the live logs during a burst is linked in the submission email |
| **Write-up** | [WRITEUP.md](WRITEUP.md): the atomic decision, idempotency, holds, CAP choice, alerting, AI usage, what's next |
| **Admin key** | needed for `POST /shows` on the live URL; sent with the submission. The local stack uses `local-dev-admin-key` |

**Latest live burst:** 20,000 requests, **0 × 5xx**, all 25 correctness checks passed, 1,244 req/s, p99 1.1s, measured from India to Singapore. See [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

## Burst it (one command)

```bash
./scripts/burst.sh https://seat-reservation-jm.up.railway.app <ADMIN_KEY>   # live
./scripts/burst.sh http://localhost:8000                                    # local stack
```

It creates a fresh show and fires **20,000 reserve requests**, 500 at a time:
- **Hot seats (40%):** 5 seats, 1,600 buyers each.
- **Overlapping multi-seat requests (15%).**
- **Racing retries (15%):** idempotency keys sent 5 times at once.
- **Per-user floods:** 50 users × 10 parallel requests against a limit of 4.
- **Spoofed identities:** a different `user_id` in the body.
- **Random seats:** the rest.

It prints throughput, latency, outcomes by status and decline reason, and the 5xx count. Then it **checks every requirement and exits 1 if any fails**:
- exactly one `201` per hot seat, and no seat sold twice;
- one booking per idempotency key, with late retries replayed and reused keys refused;
- no user above the limit, and identity taken from the token;
- the counts add up at every mid-burst snapshot and at the end;
- `/metrics` deltas equal the responses;
- the server's reconcile audit passes.

Needs [uv](https://docs.astral.sh/uv/); falls back to Docker. Flags: `--requests`, `--concurrency`, `--timeout`. Results and what tuning changed: [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

## Run it locally

Needs Docker. No `.env` is required; the stack starts with development defaults.

```bash
git clone https://github.com/jaymovaliya26/seat-reservation.git
cd seat-reservation
docker compose up --build -d --wait
curl localhost:8000/readyz
```

The local admin key is `local-dev-admin-key` unless you set `ADMIN_API_KEY` in `.env`.

## Try it

```bash
BASE=http://localhost:8000          # or the live URL
ADMIN_KEY=local-dev-admin-key       # the live admin key is shared with reviewers separately

TOKEN=$(curl -s -X POST $BASE/auth/token -H 'content-type: application/json' \
  -d '{"user_id":"alice"}' | jq -r .access_token)

SHOW=$(curl -s -X POST $BASE/shows -H "X-Admin-Key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"name":"friday-night","seats":["A11","A12","A13"],"price_paise":25000}' | jq -r .id)

# Send it twice: the retry returns the same reservation with 200 and books nothing new.
curl -s -X POST $BASE/shows/$SHOW/reserve -H "Authorization: Bearer $TOKEN" \
  -H 'Idempotency-Key: order-1' -H 'content-type: application/json' -d '{"seats":["A12"]}'

curl -s $BASE/shows/$SHOW | jq .counts

# Audit the show's books
curl -s $BASE/admin/shows/$SHOW/reconcile -H "X-Admin-Key: $ADMIN_KEY" | jq '{ok, counts}'
```

## API

| Endpoint | Auth | Result |
|---|---|---|
| `POST /auth/token` `{"user_id"}` | none | A signed JWT for that user. Stands in for an identity provider so load tests can act as many users. |
| `POST /shows` `{"name", "seats", "price_paise", "per_user_limit"?}` | `X-Admin-Key` | `201` with every seat `available` |
| `GET /shows/{id}` | none | Each seat's status, plus `counts` where `available + held + confirmed == total` |
| `POST /shows/{id}/reserve` `{"seats", "idempotency_key"?}` | `Bearer` | `201` confirmed; `200` replay of the same key; `409 seat_taken` (all-or-nothing), `409 per_user_limit`, `409 idempotency_key_reused`; `422 unknown_seats`; `404` |
| `GET /reservations/{id}` | `Bearer` | Your own reservation; anyone else's is `404` |
| `POST /reservations/{id}/cancel` | `Bearer` | `200` with status `cancelled`, seats back on sale at once; `409 already_cancelled`; `404` if not yours |
| `GET /admin/shows/{id}/reconcile` | `X-Admin-Key` | Audits the show's books in one consistent snapshot: `ok` plus seven checks (counts add up, no seat sold twice, seats match reservations, holdings match seats, limits, exact amounts) |
| `GET /metrics` | none | Prometheus metrics: outcomes by reason, live seat gauges, latency, pool use |
| `GET /healthz` | none | `200` while the process is alive |
| `GET /readyz` | none | `200` if Postgres answers within 1s, else `503` |

The idempotency key can go in the `Idempotency-Key` header or the `idempotency_key` field. Keys are scoped per user. A retry with the same key returns the original reservation (`200`, header `Idempotent-Replayed: true`); the same key with different seats is `409`. A request without a key still works but is not retry-safe.

Each user may hold at most `per_user_limit` seats per show (default 4), enforced even when the user sends many requests in parallel.

Money is always integer paise; `250.5`, `25000.0` and `"25000"` are rejected. Identity comes only from the token: a `user_id` in a request body is ignored. If Postgres can't be reached, requests fail closed with `503 database_unavailable` and `Retry-After: 1`; retrying with the same idempotency key is always safe. Every error has the same shape:

```json
{"error": {"code": "seat_taken", "message": "Seats already taken: A12", "seats": ["A12"]}, "request_id": "..."}
```

## How a seat is decided

One SQL statement in [`app/services/reservations.py`](app/services/reservations.py) locks the requested seats that are still `available`, in label order, and confirms them:

- **No double sale.** A buyer queued behind another buyer's lock re-checks `status = 'available'` once that lock is released. If the seat was sold, it drops out and the buyer gets `409`.
- **No deadlocks.** Every transaction locks seats in the same order.
- **All or nothing.** If any requested seat is gone, the transaction rolls back and the buyer holds nothing.
- **The database enforces it too.** One row per seat, and a CHECK constraint ties status to exactly one owner.
- **Retries book once.** The reservation insert waits on a unique `(user_id, idempotency_key)` index while a twin request is in flight, then replays it.
- **Limits can't be raced.** A conditional upsert on the user's holdings row makes one user's parallel requests take turns.

- **A cancel can't free someone else's seat.** Seats are released by matching their `reservation_id`, so a late or repeated cancel never touches a seat that has since been resold.

Every transaction takes its locks in the same order: the idempotency key (or the reservation row for a cancel), then holdings, then seats by label.

[`tests/test_concurrency.py`](tests/test_concurrency.py) proves this with 500 parallel buyers for one seat and 300 buyers with overlapping multi-seat requests; [`tests/test_idempotency.py`](tests/test_idempotency.py) and [`tests/test_user_limit.py`](tests/test_user_limit.py) cover 50 parallel retries and 10 parallel requests against a limit of 4.

## Observe it

- **Metrics:** `GET /metrics`. Totals add up across all Gunicorn workers, and the seat gauges are read from Postgres, so they always match the API. During a burst: `watch -n1 "curl -s $BASE/metrics | grep -E '^(reservations_|seats_)'"`.
- **Logs:** one JSON line per request with `request_id` (also returned in the `X-Request-ID` header and every error body), `user_id`, `show_id`, outcome and error code. They're capped at 50 request lines/s per worker, well under Railway's ~500 lines/s limit; anything skipped is summarised each second, and 5xx lines are always kept. Live: `railway logs --service app`.
- **Audit:** `GET /admin/shows/{id}/reconcile`.
- **Alerts:** rules in [`ops/prometheus/alerts.yml`](ops/prometheus/alerts.yml). Run Prometheus locally with `docker compose --profile observability up -d` (port 9090).
- **Smoke test any deployment:** `scripts/smoke.sh <BASE_URL> <ADMIN_KEY>` races 20 buyers for one seat and checks that the metrics and the audit agree with the outcomes.

Details, including what pages at 2am: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

## Repository map

| Path | What's there |
|---|---|
| [`app/services/reservations.py`](app/services/reservations.py) | The seat decision: fast decline, idempotency, holdings, the locked claim, cancel |
| [`app/services/reconcile.py`](app/services/reconcile.py) | The seven-check audit |
| [`migrations/`](migrations) | Schema, in order; the constraints that make a double sale unstorable |
| [`app/observability/`](app/observability) | Metrics (multiprocess), JSON logs, request IDs, log budget |
| [`tests/`](tests) | 128 tests against a real Postgres, including 500-connection races |
| [`scripts/burst.sh`](scripts/burst.sh), [`scripts/smoke.sh`](scripts/smoke.sh) | Load and correctness checks for any deployment |
| [`ops/prometheus/`](ops/prometheus) | Alert rules and a local Prometheus |
| [`docs/`](docs) | Observability, performance, roadmap, AI usage log |

## Development

```bash
make install     # uv sync
make test        # starts Postgres in Docker (port 5433), runs pytest
make check       # lint, type check, tests: what CI runs
make up          # full stack in Docker, as deployed
make run         # API with auto-reload on :8000
```

Stack: Python 3.12, FastAPI, asyncpg, PostgreSQL 18, Gunicorn + Uvicorn workers, Docker, Railway.
