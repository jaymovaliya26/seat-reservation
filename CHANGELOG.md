# Changelog

All notable changes to this project are recorded here. Releases are tagged in git.

## Unreleased

### Fixed
- The v0.2.0 build reported version 0.1.0 in its logs and OpenAPI document; the version was not bumped at release. A test now checks that `app.__version__` matches `pyproject.toml`.

## v0.2.0 (2026-10-04): safe retries and fair limits

### Added
- Idempotency keys (`Idempotency-Key` header or `idempotency_key` field), scoped per user. A retry returns the original reservation with `200` and `Idempotent-Replayed: true`; the same key with a different request is `409 idempotency_key_reused`. Exactly once even when the retries race, via a unique index the second insert waits on.
- Per-user seat limit per show (`per_user_limit`, default 4): `409 per_user_limit` with `limit`, `held` and `requested`, enforced by a conditional upsert on the user's holdings row.
- Automatic retry of transactions Postgres aborts as a deadlock or serialization failure.
- Migrations 0002 and 0003, expand-only and backward compatible: the v0.1 code passes the full suite on the new schema, and existing bookings are backfilled.
- 16 new tests (85 total), including 50 parallel retries and 10 parallel requests on a limit of 4.

### Changed
- A `user_id` in a reserve body that differs from the token is now logged (still ignored).

## v0.1.0 (2026-10-04): MVP, live

Live at https://seat-reservation-jm.up.railway.app (Railway, Singapore).

### Added
- `POST /shows/{id}/reserve`: all-or-nothing seat reservation decided by one SQL statement. Seats are locked in label order and claimed only while `status = 'available'`, so a race has exactly one winner and losers get `409 seat_taken`.
- `POST /shows` (admin key) and `GET /shows/{id}` with per-seat status and counts that always add up.
- `POST /auth/token`: signed HS256 tokens; every user endpoint takes identity from the token only.
- `/healthz` (process alive) and `/readyz` (Postgres reachable within 1s, else 503).
- JSON logs with an `X-Request-ID` on every response, and one error format for every 4xx and 5xx.
- SQL migrations applied at startup under a Postgres advisory lock; schema constraints that make a double sale impossible to store.
- Docker image (Gunicorn + 4 Uvicorn workers, non-root), docker-compose, Makefile.
- CI: lint, strict type check, 69 tests (including 500-connection concurrency tests) against Postgres, and a clean container boot.
- Railway config: health-checked, zero-downtime deploys in Singapore.
