# Seat Reservation at Scale: Write-up

**Jaykumar Movaliya** · **Live:** https://seat-reservation-jm.up.railway.app · **Repo:** https://github.com/jaymovaliya26/seat-reservation

**Stack:** Python 3.12, FastAPI, asyncpg, PostgreSQL 18, Gunicorn + Uvicorn, Docker, Railway (Singapore). **One-command burst:** `./scripts/burst.sh <URL> <ADMIN_KEY>`

## At a glance

| What was checked | Result |
|---|---|
| **Correctness under a 20,000-request burst** against the live URL (India → Singapore) | **0 × 5xx, 25/25 checks**, about 1,300 req/s, p99 about 1.1 s |
| Hot seats: 1,600 buyers on each of 5 seats | **Exactly one `201` per seat**, every other buyer a clean `409` |
| Idempotency: 600 keys, each sent 5 times at once | **Exactly 600 bookings**; late retries replay; a reused key is refused |
| Per-user limit: 50 users × 10 parallel requests, limit 4 | **Never above 4** |
| Identity: the body claims to be another user | **Ignored**; every booking belongs to the token's user |
| Reconciliation: `available + held + confirmed == total` | **Held in every snapshot** taken during the burst, and at the end |
| Metrics vs responses | **`/metrics` deltas equal the client-observed outcomes, exactly** |
| Resilience | Hard restart of the live app → ready again in about 2 s. Postgres stopped under the Docker stack → `503` with `Retry-After`, recovers without a restart. Two instances sharing Postgres, as in a deploy overlap (local) → no 5xx |
| Delivery | 7 tagged releases, each deployed and verified live; 132 tests against a real Postgres; CI on every push; interactive API docs at `/docs` |

## How I approached it

Four principles shaped every decision.

1. **The database is the only judge.** Every "who gets this seat" decision is made by Postgres in a single statement guarded on the seat's current state. No read-then-write happens in application code. The schema refuses a double sale even if the code had a bug.

2. **Ship a working system early, then add one capability at a time.** A correct MVP was live on a public URL first. After it came, in order: idempotency and the per-user limit, cancel and the reconciliation audit, observability, then load hardening. Every release was tagged, deployed, and verified against the live URL before the next began. The commit history shows that progression.

3. **Measure before claiming.** The burst script checks every requirement in the brief and exits non-zero on any failure. It was run against the live deployment and against a local copy constrained to the platform's CPU limits. Several of the design changes below exist because a measurement contradicted an assumption.

4. **Fail closed, and say so clearly.** When the system can't be certain, it refuses, and it tells the client how to retry safely. For a ticketing system, a brief outage is recoverable; selling seat A12 twice is not.

## 1. The atomic decision

**The mechanism.** One statement decides, under row locks:

```sql
WITH claim AS (
    SELECT show_id, label FROM seats
     WHERE show_id = $1 AND label = ANY($2) AND status = 'available'
     ORDER BY label
       FOR UPDATE
)
UPDATE seats s SET status = 'confirmed', reservation_id = $3, user_id = $4
  FROM claim WHERE s.show_id = claim.show_id AND s.label = claim.label
RETURNING s.label;
```

**Why it's race-free.** The check and the write are one statement, under a row lock.
- When 1,600 buyers want H1, one transaction locks the row and the rest queue behind it.
- When the winner commits, Postgres (at READ COMMITTED) re-evaluates `status = 'available'` against the newly committed row before a waiter may take it. This is the EvalPlanQual recheck.
- The seat is now `confirmed`, so it drops out of every waiter's result. Each waiter claims fewer seats than it asked for, rolls back, and returns `409 seat_taken`.
- There is no gap between "is it free?" and "take it".

**Why wait instead of `SKIP LOCKED`.** If the first claimer rolls back (it hit its per-user limit, say), the next waiter still gets the seat. With `SKIP LOCKED` or `NOWAIT`, every buyer could walk away and the seat would end with no winner.

**The schema backstops it.** One row per seat (primary key `(show_id, label)`), and a CHECK constraint that a held or confirmed seat has exactly one owner and an available seat has none. Because each seat has exactly one status, `available + held + confirmed == total` holds by construction.

**Multi-seat requests are all-or-nothing.** If the statement returns fewer rows than requested, the transaction raises. That rolls back the reservation row, the holdings increment, and every seat it did lock. The response is a `409` naming the seats that were taken.

**No deadlocks.** Every write path takes locks in one global order:
1. the idempotency key (or the reservation row, for a cancel);
2. the user's holdings row;
3. seats sorted by label (`ORDER BY label FOR UPDATE`).

Two transactions can therefore never each hold what the other is waiting for. The burst sends 3,000 overlapping 2–3-seat requests in random order without a single deadlock. As a backstop, a transaction that Postgres aborts with `40P01` or `40001` is retried up to 3 times, which is safe because it changed nothing.

**Fast decline.** About 77% of a burst's requests lose, so losers take a cheaper path: one lock-free read declines a request whose seats already belong to someone else.
- It can't double-sell. A stale read can only err in the safe direction: a seat seen as taken really was taken at that moment, and a seat seen as free still goes through the locked claim.
- It cut Postgres CPU per request by 44%.

## 2. Idempotency

**Where the key lives.** In the `reservations` row, under `UNIQUE (user_id, idempotency_key)`, next to `request_hash`, a SHA-256 of the show and the sorted seats. The row is written in the **same transaction** as the seats, so a key exists exactly when its booking exists.

**How exactly-once is enforced.**
1. **Ordinary retries:** an indexed read finds the stored reservation and replays it, with no write transaction.
2. **Racing retries:** `INSERT … ON CONFLICT (user_id, idempotency_key) DO NOTHING`. While a twin with the same key is in flight, the insert **waits on the unique index**. If the twin commits, this insert hits the conflict and replays the twin. If the twin rolls back, this one proceeds.

**Responses.**
- The first request gets `201`. A replay gets **`200`** with the original body and `Idempotent-Replayed: true`.
- Replays deliberately don't return `201`, so "exactly one `201` per seat" stays true, and `/metrics` counts them as `declined{reason="idempotent_replay"}`.

**Same key, different body.** The hash differs, so the request gets `409 idempotency_key_reused` and nothing moves. Seats are sorted before hashing, so seat order doesn't matter; a different show counts as a different body.

**Edge cases, decided deliberately.**
- **Declined requests store nothing**, so the key stays usable.
- **Keys are scoped per user**, so users can't collide or see each other's bookings.
- **After a cancel**, the key stays bound to the cancelled reservation. A late retry replays it and never books again.
- **A request without a key** is accepted and counted in a metric, but isn't retry-safe. Rejecting it would turn a missing header into a failed purchase.

## 3. Holds and expiry

**Model: immediate confirm, plus owner-only cancel.** It matches the response contract (`"status": "confirmed"`), and with no payment step in scope it minimises the states that can race.

**Cancel** (`POST /reservations/{id}/cancel`):
- **Owner only.** Anyone else gets `404`, so the reservation's existence never leaks. A second cancel gets `409 already_cancelled`.
- **It can't free a resold seat.** Seats are freed with `WHERE reservation_id = <this one>`. If Alice cancels, Bob rebooks A12, and Alice's cancel arrives again, A12 now carries Bob's reservation ID and is untouched.
- **Holdings drop in the same transaction.** Racing cancels take turns on the reservation row, so only one decrements.

**Timed holds, designed for.** The schema already has `held` and `held_until`.
- **Reserve** creates `held` with `held_until = now() + 5 min`.
- **The claim condition** becomes `status = 'available' OR (status = 'held' AND held_until < now())`, so an expired hold is reclaimable inline and correctness never depends on a sweeper.
- **`POST /reservations/{id}/confirm`** promotes `held` to `confirmed` only for the owner and only before expiry.
- **A sweeper** returns expired holds to `available` purely for display.
- **The subtle part is the per-user counts.** Reclaiming an expired hold would decrement *another* user's holdings row and break the lock order. So I'd derive live held seats from the `seats` rows rather than keep a separate counter for holds.

## 4. Consistency vs availability under a partition

**I choose consistency.** One Postgres primary is the only authority on who owns a seat. No cache, replica or in-process state ever answers "you got it".

**When the app can't reach Postgres:**
- Requests get **`503 database_unavailable` with `Retry-After: 1`**; database timeouts get `503 database_busy`.
- `/readyz` returns `503`, so no new deploy is routed traffic. `/healthz` stays `200`, so the platform doesn't restart a healthy process in a loop.
- **Verified with a real outage:** Postgres was stopped under the running Docker stack. The service answered clear 503s with no unhandled errors, and once Postgres was back, a retry with the same key booked exactly once.

**If the connection drops mid-transaction:**
- **Before COMMIT,** Postgres rolls the transaction back.
- **During COMMIT** (outcome unknown), the client's retry with the same key replays the committed booking or makes it fresh. It never books twice.

**The trade-off.** During an outage nobody can buy. For assigned seating that's the right call.

## 5. Observability: what I'd be paged for at 2am

**Metrics** (`GET /metrics`). Gunicorn runs 4 worker processes, so the counters use Prometheus multiprocess mode and are summed across all of them. Seat gauges are read from Postgres at scrape time, so they can't drift from the API.
- **Counters:** reservations confirmed, declined by reason, cancelled.
- **Gauges:** seats available, held and confirmed per show, plus `seats_invariant_ok`.
- **Request and database health:** latency by route, database pool use, transaction retries.

**Logs.** One JSON line per request with `request_id`, which is also returned in a header and in every error body.
- Railway silently drops lines above roughly 250 per second per deployment, so each worker writes at most 25 request lines per second.
- 5xx lines are always written.
- Skipped lines are summarised each second and counted.

In the final live burst, the platform kept every line written.

**What pages** (rules in [`ops/prometheus/alerts.yml`](ops/prometheus/alerts.yml), validated with promtool):
- **`SeatInvariantBroken`, immediately.** The counts don't add up, so seats or money are wrong. It is the only alert with no grace period.
- **5xx above 0.5% for 5 minutes.** Declines are 4xx by design, so any steady 5xx rate is a real failure.
- **The service unreachable for 2 minutes.**
- **Reserve p99 above 1 s for 10 minutes.**

**Tickets, not pages:** pool saturation, and a rising transaction-retry rate (the lock order should keep it at zero).

**Deliberately not alerts:** spikes in `seat_taken` or `per_user_limit`. During an on-sale, those are the system working.

## 6. What production-style testing caught

Each of these was found by measurement, against the live deployment or a local copy limited to the platform's 2 CPUs, before any reviewer could hit it. Each now has a test, a metric, or a documented number ([docs/PERFORMANCE.md](docs/PERFORMANCE.md)).

1. **Losing requests were expensive.** Each loser paid for a full write transaction. Adding the fast decline cut Postgres CPU per request by 44% and raised throughput by 26%.
2. **A deploy could cause 500s.** Pools grew lazily to 68 connections per instance, and during a deploy the old and new instances together could exceed Postgres's 100 connections (reproduced: 7,155 × 500). Pools are now 8 per worker and opened at startup, and refused connections are retried. The same scenario now passes cleanly.
3. **The platform dropped about 28,800 log lines during a burst.** A per-worker log budget aligned to wall-clock seconds brought this to zero.
4. **A real outage returned a generic 500**, because the database hostname stopped resolving. Now it's an explicit `503` with `Retry-After`.

**Net effect on the live 20,000-request burst:** throughput went from 825 to about 1,300 req/s, p99 from 1.7 s to about 1.1 s, and the maximum from 5.6 s to under 2 s. Every run had 0 × 5xx and passed all 25 checks.

## 7. How I used AI

I built this with **Claude Code (Claude Opus 5.5)** as a pair programmer. To be specific, as the brief asks: it wrote most of the code, tests, scripts and docs, and it ran deploys and load tests through CLIs I authorised. Commits it authored carry a `Co-Authored-By` trailer, and [docs/AI_USAGE.md](docs/AI_USAGE.md) logs each step.

**What I owned:**
- **The bar and the plan.** I set the target: robust and scalable under the stampede, not just functional. I chose a plan that put a correct MVP live first and then added one capability per release, each verified against the live URL before the next began.
- **The decisions:**
  - the stack (Python, FastAPI, PostgreSQL) and the platform (Railway, Singapore region);
  - the reservation model (immediate confirm plus owner-only cancel, all-or-nothing multi-seat);
  - the production calls: shrinking the connection pool, and removing a misconfigured duplicate deployment.
- **Review and release control.** Work landed in small commits and stopped at each milestone for a design walkthrough. Nothing was pushed or deployed without my go-ahead.
- **Verification as the standard of proof.** No claim in this write-up rests on "it should work". Each one is backed by the tests, the burst script's 25 checks, or a live measurement.

**What the AI contributed under that direction:**
- the implementation;
- design proposals that I reviewed and accepted: the single-statement claim with ordered locks, unique-index idempotency, the holdings row, the fast decline, startup-opened pools, the log budget, and the 503 mapping;
- the diagnosis of issues that load testing surfaced.

**What I'd take from it.** When code is cheap to produce, verification becomes the scarce resource. Several first assumptions turned out to be wrong: the platform's CPU limit, its two different logging limits, and connection behaviour during a deploy overlap. Measurement caught every one, not review. That's why the burst script and the live checks got as much attention as the API itself.

## 8. What I'd do next

1. **Timed holds and a confirm step**, as designed in section 3.
2. **Scale out:** more replicas, with Prometheus discovering and summing each one; PgBouncer in transaction mode; and a higher CPU limit for the app.
3. **A waiting room** (admission control) for very large on-sales, so the database only ever sees as many buyers as it can serve.
4. **Read path:** serve `GET /shows/{id}` from a short-TTL cache or a read replica. Bookings stay on the primary.
5. **Operations:**
   - a Grafana dashboard and hosted Prometheus with these alert rules;
   - public log search;
   - the outage test automated in CI;
   - moving `railway.json` to Railway's new config format before its 2026-12-01 deprecation.
6. **Per-user rate limiting**, returning `429`, to blunt bots before they reach the per-user limit.
