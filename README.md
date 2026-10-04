# Seat Reservation

[![CI](https://github.com/jaymovaliya26/seat-reservation/actions/workflows/ci.yml/badge.svg)](https://github.com/jaymovaliya26/seat-reservation/actions/workflows/ci.yml)

A JSON API that sells assigned seats for a show and guarantees each seat is sold exactly once, even when thousands of buyers try to book the same seat in the same second.

**Live:** https://seat-reservation-jm.up.railway.app (Railway, Singapore)

> Status: **v0.1.0**, the MVP. Idempotency keys, per-user limits, cancellation, metrics and the burst script arrive in the next releases; see [docs/ROADMAP.md](docs/ROADMAP.md).

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

curl -s -X POST $BASE/shows/$SHOW/reserve -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"seats":["A12"]}'

curl -s $BASE/shows/$SHOW | jq .counts
```

## API

| Endpoint | Auth | Result |
|---|---|---|
| `POST /auth/token` `{"user_id"}` | none | A signed JWT for that user. Stands in for an identity provider so load tests can act as many users. |
| `POST /shows` `{"name", "seats", "price_paise", "per_user_limit"?}` | `X-Admin-Key` | `201` with every seat `available` |
| `GET /shows/{id}` | none | Each seat's status, plus `counts` where `available + held + confirmed == total` |
| `POST /shows/{id}/reserve` `{"seats"}` | `Bearer` | `201` confirmed, or `409 seat_taken` (all-or-nothing), `422 unknown_seats`, `404` |
| `GET /healthz` | none | `200` while the process is alive |
| `GET /readyz` | none | `200` if Postgres answers within 1s, else `503` |

Money is always integer paise; `250.5`, `25000.0` and `"25000"` are rejected. Identity comes only from the token: a `user_id` in a request body is ignored. Every error has the same shape:

```json
{"error": {"code": "seat_taken", "message": "Seats already taken: A12", "seats": ["A12"]}, "request_id": "..."}
```

## How a seat is decided

One SQL statement in [`app/services/reservations.py`](app/services/reservations.py) locks the requested seats that are still `available`, in label order, and confirms them:

- **No double sale.** A buyer queued behind another buyer's lock re-checks `status = 'available'` once that lock is released. If the seat was sold, it drops out and the buyer gets `409`.
- **No deadlocks.** Every transaction locks seats in the same order.
- **All or nothing.** If any requested seat is gone, the transaction rolls back and the buyer holds nothing.
- **The database enforces it too.** One row per seat, and a CHECK constraint ties status to exactly one owner.

[`tests/test_concurrency.py`](tests/test_concurrency.py) proves this with 500 parallel buyers for one seat and 300 buyers with overlapping multi-seat requests.

## Development

```bash
make install     # uv sync
make test        # starts Postgres in Docker (port 5433), runs pytest
make check       # lint, type check, tests: what CI runs
make up          # full stack in Docker, as deployed
make run         # API with auto-reload on :8000
```

Stack: Python 3.12, FastAPI, asyncpg, PostgreSQL 18, Gunicorn + Uvicorn workers, Docker, Railway.
