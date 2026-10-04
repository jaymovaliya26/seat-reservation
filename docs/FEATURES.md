# Features

What this service does, explained twice: **in plain words** for anyone, and **technically** for engineers. Each feature also lists where it lives in the code and what proves it works.

**Live:** https://seat-reservation-jm.up.railway.app · **Try every endpoint:** [/docs](https://seat-reservation-jm.up.railway.app/docs)

## The service in one paragraph

**In plain words.** Think of the moment tickets for a big concert go on sale. Thousands of people press "book" in the same second, and many of them want the same few front-row seats. This service is the box office's single source of truth. It decides, instantly and fairly, who gets each seat. It never sells the same seat twice and never charges anyone twice. It also tells each unlucky buyer, politely, that the seat is gone. It keeps working while being hammered, and it shows its operators exactly what is happening.

**Technically.** A JSON HTTP API (Python 3.12, FastAPI, asyncpg) in front of a single PostgreSQL 18 database, which makes every allocation decision atomically. It runs as Gunicorn with 4 Uvicorn worker processes in a Docker container on Railway (Singapore). Prometheus metrics, structured JSON logs and an audit endpoint make its correctness observable. A one-command load test reproduces an on-sale stampede and checks every guarantee.

## Contents

- **Booking seats:** [create a show](#1-create-a-show) · [see the seat map](#2-see-the-seat-map) · [get a login token](#3-get-a-login-token) · [reserve seats](#4-reserve-seats) · [no seat is ever sold twice](#5-no-seat-is-ever-sold-twice) · [safe retries](#6-safe-retries-idempotency) · [per-person seat limit](#7-per-person-seat-limit) · [cancel a booking](#8-cancel-a-booking) · [nobody can act as someone else](#9-nobody-can-act-as-someone-else) · [exact money](#10-exact-money)
- **Keeping the books right:** [counts always add up](#11-the-counts-always-add-up) · [the audit](#12-the-audit-reconcile) · [rules enforced by the database](#13-rules-enforced-by-the-database)
- **Staying up under load:** [quick "no" for lost causes](#14-a-quick-no-for-lost-causes-fast-decline) · [show details kept in memory](#15-show-details-kept-in-memory) · [database connections budgeted](#16-database-connections-budgeted) · [automatic retry of safe failures](#17-automatic-retry-of-safe-failures) · [failing safely](#18-failing-safely-when-the-database-is-down) · [health checks](#19-health-checks) · [deploys and restarts without drama](#20-deploys-and-restarts-without-drama)
- **Seeing what's happening:** [metrics](#21-metrics) · [logs with a tracking number](#22-logs-with-a-tracking-number) · [log budget](#23-log-budget) · [alerts](#24-alerts)
- **For developers and reviewers:** [interactive API docs](#25-interactive-api-docs) · [one-command stampede test](#26-one-command-stampede-test) · [smoke test](#27-smoke-test) · [automated tests and CI](#28-automated-tests-and-ci) · [run it locally](#29-run-it-locally-in-one-command) · [database migrations](#30-database-migrations)
- [Not included (yet)](#not-included-yet) · [Glossary](#glossary) · [Where everything lives](#where-everything-lives)

---

## Booking seats

### 1. Create a show

**In plain words.** The organiser sets up an event: a name, the list of seats (A1, A2, …), the price per seat, and how many seats one person may hold. Every seat starts out free. Only the organiser can do this, using a secret admin key.

**Technically.**
- `POST /shows` takes `{name, seats, price_paise, per_user_limit?}` and needs the `X-Admin-Key` header.
- The key is compared in constant time (`hmac.compare_digest`). A missing key gets `401`; a wrong one gets `403`.
- **Validation:**
  - 1 to 50,000 unique seat labels, each matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$`;
  - an integer price from 1 to 1,000,000,000 paise;
  - `per_user_limit` from 1 to 1,000 (default 4).
- The show row and every seat row are inserted in **one transaction**, with a single `INSERT … SELECT FROM unnest($labels) WITH ORDINALITY`. The ordinality keeps the seats in the order they were given.
- The response is `201` with every seat `available`.

**Where:** `app/api/shows.py`, `app/services/shows.py`, `app/auth.py` · **Proof:** `tests/test_shows.py`

### 2. See the seat map

**In plain words.** Anyone can look at a show and see which seats are free, held or sold, plus a summary count. The counts always add up to the size of the hall.

**Technically.**
- `GET /shows/{id}` is public.
- It returns each seat's `{label, status}` in its original order, plus `counts {total, available, held, confirmed}`.
- The counts are computed from **the same single query** as the seat list. That one snapshot is why `available + held + confirmed == total` holds even mid-burst.
- An unknown show gets `404`; a malformed id gets `422`.

**Where:** `app/services/shows.py` · **Proof:** `tests/test_shows.py`, the burst's mid-burst snapshots

### 3. Get a login token

**In plain words.** Before booking, a buyer needs a pass that says who they are. In a real product this would come from the login system. Here, a test endpoint hands out a pass for any username, so testers can pretend to be thousands of different buyers.

**Technically.**
- `POST /auth/token` with `{user_id}` returns an HS256 JWT with claims `sub`, `iat` and `exp`. It's valid for 24 hours (`JWT_TTL_S`).
- The endpoint stands in for an identity provider. The real guarantee is that **every other endpoint trusts only a validly signed token**:
  - the algorithm is pinned to HS256, so `alg: none` and algorithm-confusion tricks fail;
  - `sub`, `iat` and `exp` are required;
  - the subject must match the user-id pattern.

**Where:** `app/api/auth.py`, `app/auth.py` · **Proof:** `tests/test_auth.py` (wrong secret, expired, `alg: none`, missing expiry: all `401`)

### 4. Reserve seats

**In plain words.** A buyer asks for one or more seats. They either get **all** the seats they asked for or **none**. If even one is taken, nothing is booked and they're told which seat was the problem. On success they get a booking number and the exact amount.

**Technically.**
- `POST /shows/{id}/reserve` with a Bearer token, body `{seats, idempotency_key?}` (1 to 100 unique seats), or the key in the `Idempotency-Key` header.
- **Success:** `201` with `{reservation_id, show_id, user_id, seats, amount_paise, status: "confirmed"}`.
- **Declines:** `409` with `seat_taken`, `per_user_limit` or `idempotency_key_reused`.
- **Invalid input:** `422` (`unknown_seats` names the bad labels); a missing show gets `404`.
- **Multi-seat is all-or-nothing:** if the claim locks fewer seats than requested, the transaction raises. That rolls back the reservation row, the holdings increment, and every seat it did lock.

**Where:** `app/api/reservations.py`, `app/services/reservations.py` · **Proof:** `tests/test_reserve.py`

### 5. No seat is ever sold twice

**In plain words.** When 1,600 people click on seat H1 at the same instant, exactly one of them gets it. Everyone else is told "sorry, that seat's taken". No error, no glitch, and no second copy of the seat. If the person who was about to win drops out at the last moment, the next person in line gets the seat, so it never ends up unsold by accident.

**Technically.** One statement decides, under row locks:
```sql
WITH claim AS (SELECT show_id, label FROM seats
                WHERE show_id = $1 AND label = ANY($2) AND status = 'available'
                ORDER BY label FOR UPDATE)
UPDATE seats s SET status = 'confirmed', reservation_id = $3, user_id = $4
  FROM claim WHERE s.show_id = claim.show_id AND s.label = claim.label
RETURNING s.label;
```
- **Waiters re-check the committed row.** Requests queue on the row lock. When the winner commits, READ COMMITTED re-evaluates `status = 'available'` against the new row version (the EvalPlanQual recheck). The seat drops out for every waiter, and each gets `409 seat_taken`.
- **We wait instead of using `SKIP LOCKED`.** If the first claimer rolls back, a waiter still wins, so a seat can't end up with zero winners.
- **Deadlocks are impossible.** Every write path takes locks in one global order: idempotency key → the user's holdings row → seats sorted by label.

**Where:** `app/services/reservations.py` (`_CLAIM_SEATS`) · **Proof:**
- `tests/test_concurrency.py`: 500 buyers for one seat; 300 overlapping multi-seat buyers; the lock recheck on both commit and rollback.
- Live burst: 5 seats × 1,600 buyers, exactly one `201` each.

### 6. Safe retries (idempotency)

**In plain words.** Phones lose signal and people double-tap. If a buyer's app sends the same booking request again, they get back the **same** booking, never a second one and never a second charge. If someone reuses the same request number for a different booking, the service refuses rather than guess.

**Technically.**
- **Storage:** the key lives in the `reservations` row, under `UNIQUE (user_id, idempotency_key)`, next to a `request_hash`, a SHA-256 of the show plus the sorted seats. It's written in the **same transaction** as the seats.
- **Exactly once:**
  1. an indexed read replays ordinary retries without a write transaction;
  2. for racing duplicates, `INSERT … ON CONFLICT DO NOTHING` **waits on the unique index** until the twin commits, then replays it.
- **Responses:** a replay is `200` with the original body and `Idempotent-Replayed: true`. It's not `201`, so "one 201 per seat" stays true. The same key with a different body gets `409 idempotency_key_reused`.
- **Edge cases:**
  - keys are scoped per user;
  - a declined request stores nothing, so its key stays usable;
  - after a cancel, the key still points at the cancelled booking;
  - a request without a key works, but isn't retry-safe, and is counted in a metric.

**Where:** `app/services/reservations.py`, `migrations/0002_idempotency.sql` · **Proof:** `tests/test_idempotency.py` (50 racing retries: 1 × 201 + 49 × 200); live burst: 600 keys × 5 racing sends → 600 bookings

### 7. Per-person seat limit

**In plain words.** Nobody can grab more than their share. With a limit of 4, a person firing 10 requests at once still ends up with 4 seats at most. If a booking fails because a seat was taken, it doesn't count against their limit.

**Technically.**
- A `user_show_holdings` row per (user, show) is updated with a conditional upsert: `seat_count = seat_count + n WHERE seat_count + n <= limit`.
- The upsert's row lock makes one user's parallel requests take turns. Other users never touch that row, so there's no cross-user contention.
- If no row is updated, the answer is `409 per_user_limit` with `limit`, `held` and `requested`.
- A later `seat_taken` rolls back the increment.

**Where:** `app/services/reservations.py`, `migrations/0003_user_show_holdings.sql` · **Proof:** `tests/test_user_limit.py`; live: 50 users × 10 parallel requests, never above 4

### 8. Cancel a booking

**In plain words.** A buyer can cancel their own booking, and the seats go straight back on sale. Nobody can cancel someone else's booking, or even find out that it exists. A late "cancel" can never take away a seat that someone else has since bought.

**Technically.**
- `POST /reservations/{id}/cancel` is owner-only. Anyone else gets `404`, and a repeat cancel gets `409 already_cancelled`.
- In one transaction it does three things, in the global lock order:
  1. locks the reservation row (so racing cancels take turns);
  2. decrements the holdings;
  3. frees seats `WHERE reservation_id = <this one>`, locked in label order. A seat resold to someone else carries their reservation id, so it's untouched.
- `GET /reservations/{id}` shows your own booking; anyone else's is a 404.

**Where:** `app/services/reservations.py` (`cancel`, `_RELEASE_SEATS`) · **Proof:** `tests/test_cancel.py` (10 racing cancels → one wins; a cancel racing 30 rebooks; a stale cancel after resale)

### 9. Nobody can act as someone else

**In plain words.** The service only believes the signed pass, never what someone types into a request. If a request claims to be from "victim", it's still booked as whoever's pass was used.

**Technically.**
- Identity comes from the JWT `sub` only.
- A `user_id` field in a body is ignored. It's flagged on the request's log line and counted in `spoofed_user_id_total`.
- Reservation lookups and cancels filter by the token user.

**Where:** `app/auth.py`, `app/api/reservations.py` · **Proof:** `tests/test_reserve.py`; live burst: 100 spoofed requests, every booking owned by the token's user

### 10. Exact money

**In plain words.** Prices are kept in paise as whole numbers, so there are no rounding errors. ₹250 is stored as 25000. A price like 250.5 is rejected outright.

**Technically.**
- `price_paise` and `amount_paise` are `BIGINT`, validated as Pydantic `StrictInt`, so `250.5`, `25000.0` and `"25000"` all get `422`.
- `amount_paise = price_paise × number of seats`, and the audit checks it.

**Where:** `app/models.py`, `migrations/0001_init.sql` · **Proof:** `tests/test_shows.py`

---

## Keeping the books right

### 11. The counts always add up

**In plain words.** At every moment, free seats + held seats + sold seats = the size of the hall. Not just in theory: the load test checks this repeatedly while 20,000 requests are flying.

**Technically.**
- Each seat is exactly one row with one `status`, so the identity holds by construction.
- `GET /shows/{id}` derives the counts from one query.
- The `seats_invariant_ok{show_id}` gauge exposes it live.

**Where:** `migrations/0001_init.sql`, `app/services/shows.py`, `app/observability/metrics.py` · **Proof:** the burst's mid-burst snapshots, the final reconcile

### 12. The audit (reconcile)

**In plain words.** A one-click health check of a show's books. It cross-checks seats, bookings, per-person counts and money, and says "all good" or lists exactly what's wrong.

**Technically.** `GET /admin/shows/{id}/reconcile` (admin key) runs seven checks inside one `REPEATABLE READ`, read-only transaction, so all of them see the same instant:
- `counts_add_up`
- `no_seat_sold_twice`
- `seats_match_reservations`
- `reservations_fully_seated`
- `holdings_match_seats`
- `within_user_limit`
- `amounts_are_exact`

Each check returns `ok`, a violation count and up to 5 examples.

**Where:** `app/services/reconcile.py` · **Proof:** `tests/test_reconcile.py` (4 kinds of deliberate corruption caught; a 720-operation random storm balances)

### 13. Rules enforced by the database

**In plain words.** Even if the program had a bug, the database itself would refuse to record a seat sold twice, a sold seat with no owner, or a negative price.

**Technically.**
- Primary key `(show_id, label)`: a seat can't exist twice.
- CHECK `seats_owner_matches_status`: an available seat has no owner; a held or confirmed seat has exactly one.
- `price_paise > 0` and `seat_count >= 0`.

**Where:** `migrations/` · **Proof:** `tests/test_migrate.py` (`TestSchemaGuards`)

---

## Staying up under load

### 14. A quick "no" for lost causes (fast decline)

**In plain words.** Most people in a ticket rush don't get the seat they want. Rather than doing the full booking paperwork just to say no, the service checks first. If the seat is clearly already someone else's, it answers immediately. That frees capacity for the people who can still win.

**Technically.**
- One lock-free indexed read finds requested seats owned by another user and answers `409`.
- **Why it's safe:** a stale read can only err in the safe direction. A seat seen as taken really was taken at that moment; a seat seen as free still goes through the locked claim.
- Seats held by the same user skip this path, so a racing retry becomes a replay, never a wrong `409`.
- Result: Postgres CPU per request down 44%, throughput up 26%.

**Where:** `app/services/reservations.py` (`_TAKEN_BY_OTHERS`) · **Proof:** `tests/test_reserve.py`, [PERFORMANCE.md](PERFORMANCE.md)

### 15. Show details kept in memory

**In plain words.** A show's price, seat list and limit never change, so each server process remembers them instead of asking the database every time. When thousands of first requests for a new show arrive at once, only one of them looks the show up; the rest wait for that answer.

**Technically.**
- A per-worker `ShowCatalog` caches immutable show data, with an LRU bound of 256 shows.
- **Single-flight loading:** concurrent misses share one load (an `asyncio` task awaited via `shield`).
- Misses and failed loads aren't cached.
- No invalidation is needed, because shows are immutable.

**Where:** `app/services/catalog.py` · **Proof:** `tests/test_reserve.py` (200 concurrent first lookups → 1 load)

### 16. Database connections budgeted

**In plain words.** A database can only talk to so many callers at once. The service books its connections up front and never asks for more mid-rush. That's true even during an update, when the old and new versions run side by side for a moment.

**Technically.**
- Pools are 8 per worker and opened in full at startup (`min = max`), plus 1 connection for readiness.
- That's 36 per instance, and 72 during Railway's 15s deploy overlap, under Postgres's `max_connections` of 100.
- Session timeouts: `statement_timeout` 5s, `lock_timeout` 5s, `idle_in_transaction_session_timeout` 10s. A stuck connection can never hold a seat lock indefinitely.
- The old lazy pool reproduced 7,155 × 500 when two instances shared Postgres; the new budget passes the same test cleanly.

**Where:** `app/db.py`, `app/config.py` · **Proof:** [PERFORMANCE.md](PERFORMANCE.md)

### 17. Automatic retry of safe failures

**In plain words.** Occasionally the database says "try that again". The service does so automatically, but only when it's certain nothing happened the first time.

**Technically.**
- The whole attempt (reads included) is rerun, up to 3 times with jittered backoff, on:
  - deadlock (`40P01`);
  - serialization failure (`40001`);
  - too many connections (`53300`);
  - cannot connect now (`57P03`).
- The same idempotency key is used on every attempt, so a rerun after an unclear failure replays rather than double-books.
- Retries are counted in `db_transaction_retries_total{sqlstate}`.

**Where:** `app/db.py` (`with_retries`) · **Proof:** `tests/test_db_retry.py`

### 18. Failing safely when the database is down

**In plain words.** If the database becomes unreachable, the service doesn't guess and doesn't sell from out-of-date information. It says clearly, "temporarily unavailable, try again in a second." Trying again with the same request number is always safe.

**Technically.**
- `503 database_unavailable` (connection errors, refused connections, and any `OSError`, since Postgres is the only network I/O on a request path) or `503 database_busy` (statement or lock timeout).
- Both carry `Retry-After: 1` and the `request_id`.
- This is the consistency-over-availability choice.

**Where:** `app/errors.py` · **Proof:** `tests/test_fail_closed.py`; a manual outage test (stop Postgres: 503s, no unhandled errors; restart: the same key books once)

### 19. Health checks

**In plain words.** Two quick questions the hosting platform can ask: "Are you alive?" and "Are you ready to serve customers?" The second answer is honest. If the database is down, the service says it isn't ready.

**Technically.**
- `GET /healthz`: liveness. It never touches the database.
- `GET /readyz`: runs `SELECT 1` within 1s on a **dedicated** one-connection pool, so a busy main pool can't fake an outage. It returns `503` when the database is unreachable.
- Railway routes traffic to a new deploy only after `/readyz` returns 200.

**Where:** `app/api/health.py`, `app/db.py` · **Proof:** `tests/test_health.py`

### 20. Deploys and restarts without drama

**In plain words.** Updates go out without taking the service offline, and if it's restarted from cold, it's back on its feet in about two seconds without anyone lifting a finger.

**Technically.**
- **Workers:** Gunicorn supervises 4 Uvicorn workers. It restarts crashed workers and gives in-flight requests 20s to drain on SIGTERM. The connection backlog is 4096.
- **Startup:** waits for Postgres with backoff for up to 60s, then applies migrations under an advisory lock.
- **Railway:**
  - health checks on `/readyz`;
  - a 15s overlap between the old and new deploy, and 25s of draining;
  - restart on failure;
  - never sleeps.
- **Container:** non-root (uid 10001), built in two stages from `uv.lock`.

**Where:** `gunicorn.conf.py`, `railway.json`, `Dockerfile`, `app/main.py` · **Proof:** a hard restart of the live app was ready again in about 2s

---

## Seeing what's happening

### 21. Metrics

**In plain words.** A live scoreboard: how many bookings succeeded, how many were turned away and why, how many seats are left in each show, how fast responses are, and whether the counts add up. The scoreboard always agrees with what buyers were actually told.

**Technically.** `GET /metrics` serves Prometheus text format. Gunicorn's workers share counters through Prometheus multiprocess mode, so a scrape shows totals for the whole service. Seat gauges are read from Postgres at scrape time, for the 20 most recent shows. The metrics are:
- **Bookings:**
  - `reservations_confirmed_total`;
  - `reservations_declined_total{reason}`: `seat_taken`, `per_user_limit`, `idempotent_replay`, `idempotency_key_reused`, `unknown_seats`, `not_found`, `invalid_request`;
  - `reservations_cancelled_total`, `seats_sold_total`, `seats_released_total`.
- **Seats:** `seats_available` / `seats_held` / `seats_confirmed` / `seats_total` / `seats_invariant_ok`, each `{show_id}`.
- **Requests:** `http_requests_total` and `http_request_duration_seconds`, labelled by route template.
- **Database:** `db_pool_connections{state}`, `db_transaction_retries_total`.
- **Clients and logging:** `reserve_requests_without_idempotency_key_total`, `spoofed_user_id_total`, `log_lines_suppressed_total`.
- **Version:** `app_info{version}`.

**Where:** `app/observability/metrics.py`, `gunicorn.conf.py` · **Proof:** `tests/test_metrics.py`; the burst checks that metric deltas equal client-observed outcomes exactly

### 22. Logs with a tracking number

**In plain words.** Every request gets a tracking number. It's written in the log, returned to the caller, and included in every error message. If a buyer reports a problem, support can find exactly what happened.

**Technically.**
- One JSON line per request, on stdout: `request_id`, `method`, `path`, `route`, `status`, `duration_ms`, `user_id`, `show_id`, `outcome`, `error_code`.
- The `X-Request-ID` header is accepted if it's valid, otherwise generated, and returned on every response.
- Gunicorn, Uvicorn and asyncpg lines use the same JSON shape.
- Unhandled exceptions become a JSON `500` with the request id and a logged stack trace.

**Where:** `app/observability/logging.py`, `app/observability/middleware.py` · **Proof:** `tests/test_health.py`

### 23. Log budget

**In plain words.** The hosting platform throws away log lines if you write too many too fast, which is exactly what would happen during a ticket rush. So the service writes a sensible number of lines and summarises the rest, instead of letting the platform lose lines at random. Errors are always written.

**Technically.**
- Each worker writes at most `LOG_REQUEST_LINES_PER_SECOND` request lines (default 25), counted per wall-clock second. That's 100/s across 4 workers, under Railway's limits of 500/s per replica and about 250/s per deployment.
- 5xx lines bypass the cap.
- Skipped lines become one `request_lines_suppressed` summary per second, by status, and are counted in `log_lines_suppressed_total`.
- Live, the dropped lines went from about 28,800 to zero.

**Where:** `app/observability/log_budget.py` · **Proof:** `tests/test_log_budget.py`, [PERFORMANCE.md](PERFORMANCE.md)

### 24. Alerts

**In plain words.** The rules for when to wake an engineer at 2am, and when something can wait until morning. If the seat counts ever don't add up, someone is paged immediately.

**Technically.** Six Prometheus alert rules, validated with `promtool`:
- **Page, immediately:** `SeatInvariantBroken`.
- **Page after a short grace period:**
  - `ServerErrors`: 5xx above 0.5% for 5 minutes;
  - `ServiceDown`: scrapes failing for 2 minutes;
  - `ReserveLatencyHigh`: reserve p99 above 1s for 10 minutes.
- **Tickets:** `DatabasePoolSaturated`, `TransactionRetriesRising`.

Spikes in `seat_taken` deliberately aren't alerts. `docker compose --profile observability up` runs Prometheus with the rules loaded.

**Where:** `ops/prometheus/`, [OBSERVABILITY.md](OBSERVABILITY.md)

---

## For developers and reviewers

### 25. Interactive API docs

**In plain words.** A web page that lists every operation and lets you try each one from the browser. You log in once with the Authorize button, then click Execute.

**Technically.**
- Swagger UI at `/docs` and ReDoc at `/redoc`, generated from OpenAPI 3.1.
- The token (`BearerAuth`) and admin key (`AdminKey`) are declared security schemes, so Authorize works.
- Every error status is documented in the real error format, with examples.
- Request bodies have example values.
- `docs/openapi.json` is committed, valid, and kept in sync by a test (`make openapi` regenerates it).

**Where:** `app/api/openapi.py`, `scripts/export_openapi.py` · **Proof:** `tests/test_openapi.py`; the Authorize flow was checked in a browser

### 26. One-command stampede test

**In plain words.** One command recreates an on-sale rush against any copy of the service, then checks every promise above and reports pass or fail.

**Technically.**
- `./scripts/burst.sh <URL> [ADMIN_KEY]` (or `make burst`) creates a fresh show and fires 20,000 reserve requests, 500 at a time:
  - **hot seats (40%):** 5 seats × 1,600 buyers;
  - **overlapping multi-seat requests (15%);**
  - **racing retries (15%):** 600 keys × 5 sends;
  - **per-user floods:** 50 users × 10 parallel requests at a limit of 4;
  - **spoofed identities;**
  - **random seats:** the rest.
- It prints throughput, latency, outcomes by status and reason, and the 5xx count.
- It then runs **25 checks**, including the metric deltas and the reconcile audit, and exits `1` on any failure.
- It's a self-contained script (PEP 723 inline dependencies) that runs with `uv`, with a Docker fallback.

**Where:** `scripts/burst.py`, `scripts/burst.sh` · **Proof:** live runs: 0 × 5xx, 25/25

### 27. Smoke test

**In plain words.** A quick 20-person race for one seat that confirms the basics on any copy of the service in seconds.

**Technically.**
- `scripts/smoke.sh <URL> <ADMIN_KEY>` requires exactly one `201` and 19 × `409`, matching metric deltas, and a clean reconcile.
- CI runs it against the 4-worker container on every push.

**Where:** `scripts/smoke.sh`, `.github/workflows/ci.yml`

### 28. Automated tests and CI

**In plain words.** 132 automatic tests run against a real database every time the code changes, including tests that make hundreds of buyers race each other.

**Technically.**
- pytest runs against PostgreSQL through a real uvicorn server, with up to 500 parallel connections.
- The suites cover units, integration, concurrency, randomized storms, corruption detection, migrations, fail-closed behaviour and OpenAPI sync.
- CI on every push:
  - ruff, ruff format, and strict mypy;
  - the test suite against Postgres 18;
  - a clean Docker build and boot with no `.env`, plus the smoke test.

**Where:** `tests/`, `.github/workflows/ci.yml`

### 29. Run it locally in one command

**In plain words.** Anyone can start the whole system on their own computer with one command, with no setup files needed.

**Technically.**
- `docker compose up --build -d --wait` starts Postgres 18 and the app exactly as deployed. Development defaults mean no `.env` is needed.
- Make targets: `make up`, `test`, `check`, `burst`, `smoke`, `openapi`, `run`.

**Where:** `docker-compose.yml`, `Makefile`, `Dockerfile` · **Proof:** a fresh clone from GitHub passed the full burst

### 30. Database migrations

**In plain words.** Changes to the database's structure are applied automatically and safely when the service starts. Even if several copies start at once, each change is applied exactly once.

**Technically.**
- Forward-only SQL files are applied at startup under a Postgres advisory lock.
- Each file runs in one transaction together with its bookkeeping row.
- A SHA-256 checksum refuses edits to already-applied files.
- Migrations are written expand-only, so the previous version keeps working during a deploy overlap.
- `python -m app.migrate` runs them by hand.

**Where:** `app/migrate.py`, `migrations/` · **Proof:** `tests/test_migrate.py` (8 concurrent startups apply once; a failed file leaves nothing behind; an upgrade from v0.1 data)

---

## Not included (yet)

| Feature | Status |
|---|---|
| Timed holds that expire, plus a confirm or payment step | Schema ready (`held`, `held_until`); design in [WRITEUP.md](../WRITEUP.md) §3 |
| Several app replicas | Needs per-replica metric scraping and PgBouncer; see [PERFORMANCE.md](PERFORMANCE.md) |
| Waiting room for very large on-sales | Next step for admission control |
| Per-user rate limiting (`429`) | Not built |
| Real login and payments | Out of scope; `/auth/token` stands in for an identity provider |

## Glossary

| Term | Plain meaning |
|---|---|
| **API** | The set of web addresses a program calls to use the service |
| **201 / 200 / 409 / 422 / 503** | HTTP answer codes: created / OK (here, a repeat of an earlier answer) / declined because of the current state / request is malformed / temporarily unavailable |
| **Idempotency key** | A request number the client attaches, so a repeated request is recognised as the same one |
| **Row lock** | The database letting only one transaction change a row at a time; others queue |
| **Transaction** | A group of database changes that all happen or none do |
| **Deadlock** | Two transactions each waiting for the other forever; prevented here by always locking in the same order |
| **Invariant** | A rule that must always be true, here available + held + confirmed = total |
| **p50 / p99 latency** | The response time that half / 99% of requests beat |
| **5xx** | A server-side error; this service is built never to produce one under load |
| **Worker** | One of the 4 server processes sharing the work |
| **Fail closed** | When unsure, refuse instead of guessing |
| **JWT** | A signed token proving who the caller is |

## Where everything lives

| Path | Features |
|---|---|
| `app/services/reservations.py` | Reserve, no double sale, idempotency, per-user limit, fast decline, cancel (4–8, 14) |
| `app/services/catalog.py` | Show details in memory (15) |
| `app/services/reconcile.py` | Audit (12) |
| `app/services/shows.py` | Create and read shows (1, 2) |
| `app/auth.py`, `app/api/auth.py` | Tokens, identity, admin key (3, 9) |
| `app/db.py` | Connection budget, retries, readiness pool (16, 17, 19) |
| `app/errors.py` | Error format, fail closed (18) |
| `app/observability/` | Metrics, logs, request IDs, log budget (21–23) |
| `app/api/openapi.py` | API docs (25) |
| `app/migrate.py`, `migrations/` | Migrations, database rules (13, 30) |
| `ops/prometheus/` | Alerts (24) |
| `scripts/` | Burst, smoke, OpenAPI export (25–27) |
| `Dockerfile`, `docker-compose.yml`, `gunicorn.conf.py`, `railway.json` | Run and deploy (20, 29) |
| `tests/`, `.github/workflows/ci.yml` | Tests and CI (28) |
