# Changelog

All notable changes to this project are recorded here. Releases are tagged in git.

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
