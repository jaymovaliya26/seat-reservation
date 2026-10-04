# Changelog

All notable changes to this project are recorded here. Releases are tagged in git.

## v1.0.0 (2026-10-04): submission

Live at https://seat-reservation-jm.up.railway.app. Final live burst: 20,000 requests, 1,369 req/s, p99 1.0 s, 0 × 5xx, 25/25 checks. A fresh clone runs with no setup; a hard restart is back in about 2 s.

### Added
- `WRITEUP.md`: the atomic decision, idempotency, holds, the consistency choice, alerting, AI usage, and next steps.
- README: a reviewer section and a repository map.

### Fixed
- When Postgres is unreachable, requests now fail closed with `503 database_unavailable` (or `503 database_busy` on a database timeout), with `Retry-After: 1`, instead of a generic 500. Found by stopping Postgres under the running stack: the database hostname stopped resolving (`socket.gaierror`).
- The log budget still let Railway drop 1,578 lines in a live burst: per-worker windows started at arbitrary moments, so a worker could write twice its cap within one wall-clock second. Windows are now whole wall-clock seconds, and the default cap is 25 lines/s per worker (100/s in total): at 50, Railway still dropped 992 lines under a second, per-deployment limit of roughly 250 lines/s.

## v0.5.0 (2026-10-04): ready for the stampede

### Added
- `./scripts/burst.sh <BASE_URL> [ADMIN_KEY]` (also `make burst`): 20,000 reserve requests across six on-sale scenarios, then 25 correctness checks; exits 1 on any failure.
- `docs/PERFORMANCE.md`: live and local measurements, and what they changed.
- Metrics: `reserve_requests_without_idempotency_key_total`, `spoofed_user_id_total`, `log_lines_suppressed_total`.

### Changed
- **Fast decline:** a request whose seats already belong to someone else gets `409` from one read instead of a write transaction (Postgres CPU per request −44%).
- **Connection budget:** pools are 8 per worker and open in full at startup (36 connections per instance, 72 during a deploy overlap, under Postgres's 100). Railway's `DB_POOL_MAX` changed from 15 to 8.
- **Log budget:** at most 100 request lines/s per worker, under Railway's 500 lines/s limit; skipped lines are summarised each second, and 5xx lines are always kept.

### Fixed
- A deploy overlap could exhaust Postgres connections and turn requests into 500s (reproduced as 7,155 × 500 in a benchmark). Refused connections are now also retried.
- Railway was silently dropping about 28,800 log lines during a burst.
- The Gunicorn control socket logged an error at boot; it's now disabled.

### Live result
20,000-request burst against the live URL: 1,130 req/s, p50 344 ms, p99 1.3 s, 0 × 5xx, 25/25 checks (v0.4 was 825 req/s, p99 1.7 s).

## v0.4.0 (2026-10-04): observability

### Added
- `GET /metrics`: reservation outcomes by reason, seat gauges and an invariant gauge per show (read from Postgres at scrape time), HTTP request counts and latency by route template, database pool use, transaction retries, version. Prometheus multiprocess mode sums all Gunicorn workers.
- JSON logs for the Gunicorn master too; request lines now carry `outcome`, `reservation_id` and `error_code`.
- `scripts/smoke.sh`: a 20-buyer race, then checks that metrics and the reconcile audit agree with the outcomes. CI runs it against the 4-worker container on every push.
- Alert rules in `ops/prometheus/alerts.yml` (validated with promtool), an optional local Prometheus (`docker compose --profile observability up`), and `docs/OBSERVABILITY.md`.
- 4 new tests (108 total).

## v0.3.0 (2026-10-04): release seats and prove the books balance

### Added
- `POST /reservations/{id}/cancel`: owner-only (others get `404`), `409 already_cancelled` on a repeat. Seats return to sale at once and are released by `reservation_id`, so a stale cancel can never free a resold seat. The user's holdings go down in the same transaction.
- `GET /reservations/{id}`: the caller's own reservation.
- `GET /admin/shows/{id}/reconcile`: seven integrity checks in one REPEATABLE READ snapshot, each with a violation count and examples.
- 19 new tests (104 total): racing cancels, a cancel racing 30 rebooks, four kinds of deliberate corruption that reconcile must catch, and a randomized storm of 720 concurrent reserves, retries and cancels that must end with no 5xx and balanced books.

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
