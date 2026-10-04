# Write-up

**Live:** https://seat-reservation-jm.up.railway.app · **Burst:** `./scripts/burst.sh <URL> <ADMIN_KEY>` · **Stack:** Python 3.12, FastAPI, asyncpg, PostgreSQL 18, Gunicorn + Uvicorn, Railway (Singapore)

## 1. The atomic decision

**Mechanism.** Postgres decides who gets a seat, in one statement:

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

**Why it's race-free.** The check and the write are the same statement, under a row lock.
- When 1,600 buyers want H1, one transaction locks the row and the rest queue behind it.
- When the winner commits, READ COMMITTED re-evaluates `status = 'available'` against the newly committed row before a waiter may take it (Postgres's EvalPlanQual recheck).
- The seat is now `confirmed`, so it drops out of every waiter's result. Each waiter claims fewer seats than it asked for, rolls back, and gets `409 seat_taken`.
- There is no point between "is it free?" and "take it" where another request can slip in.

**Why wait instead of `SKIP LOCKED`.** If the first claimer rolls back (it hit its per-user limit, say), a waiter still gets the seat. With `SKIP LOCKED` or `NOWAIT`, every buyer could walk away and the seat would end with no winner.

**The schema backstops it.** One row per seat (primary key `(show_id, label)`), and a CHECK constraint that a held or confirmed seat has exactly one owner and an available seat has none. A bug in Python still could not store a double sale. And because each seat has exactly one status, `available + held + confirmed == total` holds by construction.

**Multi-seat requests are all-or-nothing.** If the statement returns fewer rows than requested, the transaction raises. That rolls back the reservation row, the user's holdings increment, and any seats it did lock. The response is `409` naming the seats that were taken.

**No deadlocks.** Every write path takes locks in one global order:
1. the idempotency key (or the reservation row, for a cancel);
2. the user's holdings row;
3. seats sorted by label (`ORDER BY label FOR UPDATE`).

Two transactions can never each hold what the other is waiting for. The burst sends 3,000 overlapping 2–3-seat requests in random order without a deadlock. As a backstop, a transaction Postgres does abort with `40P01` or `40001` is retried up to 3 times; it changed nothing, so retrying is safe.

**Fast decline.** About 77% of a burst's requests lose, so losers get a cheaper path. One read, with no lock, declines a request whose seats already belong to someone else.
- It can't double-sell, because a stale read can only err in one direction. A seat it sees as taken really was taken at that moment, so declining is a valid answer. A seat it sees as free still goes through the locked claim.
- It cut Postgres CPU per request by 44%.

## 2. Idempotency

**Where the key lives.** In the `reservations` row, under `UNIQUE (user_id, idempotency_key)`, next to `request_hash`: a SHA-256 of the show and the sorted seats. The row is written in the **same transaction** as the seats, so a key exists exactly when its booking exists.

**How exactly-once is enforced.**
1. **Ordinary retries:** an indexed read finds the stored reservation and replays it, with no write transaction.
2. **Racing retries:** `INSERT … ON CONFLICT (user_id, idempotency_key) DO NOTHING`. While a twin with the same key is in flight, the insert **waits on the unique index**. If the twin commits, the insert hits the conflict and we replay the twin. If the twin rolls back, the insert goes ahead.

In the burst, 600 keys each sent 5 times at once produce exactly 600 bookings.

**Responses.**
- The first request gets `201`. A replay gets **`200`** with the original body and `Idempotent-Replayed: true`.
- Replays deliberately don't return `201`, so "exactly one `201` per seat" stays true. `/metrics` counts them as `declined{reason="idempotent_replay"}`.

**Same key, different body.** The hash differs, so the request gets `409 idempotency_key_reused` and nothing moves. Seat order doesn't matter, because the seats are sorted before hashing; a different show counts as a different body.

**Edge cases.**
- **Declined requests store nothing**, so the key stays usable.
- **Keys are scoped per user**, so two users can't collide or see each other's bookings.
- **After a cancel**, the key stays bound to the cancelled reservation. A very late retry replays it; it never books again.
- **A request without a key** is accepted and counted in a metric, but isn't retry-safe. Refusing it would turn a missing header into a failed purchase.

## 3. Holds and expiry

**Model chosen: immediate confirm, plus owner-only cancel.** It matches the response contract (`"status": "confirmed"`), and with no payment step in scope it keeps the number of racing states small.

**Cancel** (`POST /reservations/{id}/cancel`):
- **Owner only.** Anyone else gets `404`, so the reservation's existence is never revealed. A second cancel gets `409 already_cancelled`.
- **It can't free a resold seat.** Seats are freed with `WHERE reservation_id = <this one>`. If Alice cancels, Bob rebooks A12, and Alice's cancel arrives again, it can't touch A12: the seat now carries Bob's reservation ID.
- **The user's holdings drop in the same transaction.** Racing cancels take turns on the reservation row, so only one decrements.

**How I'd add timed holds.** The schema already has `held` and `held_until`.
- **Reserve** creates `held` with `held_until = now() + 5 min`.
- **The claim condition** becomes `status = 'available' OR (status = 'held' AND held_until < now())`, so an expired hold is reclaimable inline. Correctness never depends on a sweeper running.
- **`POST /reservations/{id}/confirm`** moves `held` to `confirmed` only for the owner and only if the hold hasn't expired.
- **A sweeper** returns expired holds to `available` purely so they display correctly.
- **The subtle part is the per-user counts.** Reclaiming an expired hold would mean decrementing *another* user's holdings row, which breaks the lock order. I'd instead derive held seats from `seats` rows still live (`held_until > now()` or confirmed) rather than keeping a separate counter for holds.

## 4. Consistency vs availability under a partition

**I choose consistency.** One Postgres primary is the only authority on who owns a seat. No cache, replica or in-process state ever answers "you got it".

**When the app can't reach Postgres:**
- Every request that needs it gets **`503 database_unavailable` with `Retry-After: 1`**. Database timeouts get `503 database_busy`.
- `/readyz` returns `503`, so no new deploy is routed traffic. `/healthz` stays `200`, so the platform doesn't restart a healthy process in a loop.
- I verified this by stopping Postgres under the running stack: clear 503s, no unhandled errors. Once Postgres was back, a retry with the same key booked exactly once.

**If the connection drops mid-transaction:**
- **Before COMMIT:** Postgres rolls the transaction back.
- **During COMMIT** (outcome unknown): the client's retry with the same key either replays the committed booking or makes it fresh. It never books twice.

**The trade-off.** During an outage nobody can buy. For assigned seating that's the right call: a brief outage is recoverable, while selling A12 twice isn't.

## 5. Observability: what I'd be paged for at 2am

**Metrics** (`GET /metrics`). Gunicorn runs 4 worker processes, so the counters use Prometheus's multiprocess mode and are summed across all of them. Seat gauges are read from Postgres at scrape time.
- **Counters:** reservations confirmed, declined by reason, cancelled.
- **Gauges:** seats available, held and confirmed per show, plus `seats_invariant_ok`.
- **Request and database health:** latency by route, database pool use, transaction retries.

The burst checks that the `/metrics` deltas equal the client-observed outcomes exactly.

**Logs.** One JSON line per request with `request_id`, which is also returned in a header and in every error body. Railway silently drops lines above roughly 250 per second per deployment, so each worker writes at most 25 request lines per second. 5xx lines are always written, and skipped lines are summarised each second and counted.

**What pages** (rules in [`ops/prometheus/alerts.yml`](ops/prometheus/alerts.yml), validated with promtool):
- **`SeatInvariantBroken`, immediately.** The counts don't add up, so seats or money are wrong. The only alert with no grace period.
- **5xx above 0.5% for 5 minutes.** Declines are 4xx by design, so any steady 5xx rate is a real failure.
- **The service unreachable for 2 minutes.**
- **Reserve p99 above 1s for 10 minutes.**

**Tickets, not pages:** pool saturation, and a rising transaction-retry rate (the lock order should make that zero).

**Deliberately not alerts:** spikes in `seat_taken` or `per_user_limit`. During an on-sale, those are the system working.

## 6. What load testing found and fixed

All of this was measured with the burst script against the live URL and against a local copy limited to Railway's 2 CPUs. Details are in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

1. **Losers were expensive.** Each paid for a full write transaction, which led to the fast decline: Postgres CPU per request −44%, throughput +26%.
2. **A deploy could cause 500s.** Pools grew lazily to 68 connections per instance, and old plus new instances during a deploy could exceed Postgres's 100 connections (reproduced: 7,155 × 500). Pools are now 8 per worker, opened at startup, and refused connections are retried.
3. **Railway dropped about 28,800 log lines** during a burst. That led to the log budget, with windows aligned to wall-clock seconds.
4. **A real outage returned a generic 500**, because the database hostname stopped resolving. That led to the explicit `503 database_unavailable`.

Live, the 20,000-request burst moved from 825 to 1,244 req/s and p99 from 1.7s to 1.1s, with **0 × 5xx and all 25 checks passing**.

## 7. AI usage: directed vs decided

I used **Claude Code (Claude Opus 5.5)** throughout. It wrote nearly all of the code, tests, scripts and docs, and it ran the deploys and load tests through the GitHub and Railway CLIs, which I authorised. Every commit it made carries a `Co-Authored-By: Claude` trailer. The per-step log is in [docs/AI_USAGE.md](docs/AI_USAGE.md).

**What I decided:**
- The stack (Python and FastAPI), the host (Railway), and the hold model (confirm plus cancel).
- An MVP-first plan with one release per capability.
- The working mode: the AI implements in small commits, stops at each milestone to explain, and **pushes only when I say so**. Every push and every production change was approved by me: deleting a duplicate Railway project, and changing `DB_POOL_MAX`.
- Committing under my personal email, not my employer's.

**What the AI proposed and I accepted after review:**
- the single-statement claim with ordered locks;
- idempotency via a unique index plus a request hash;
- the holdings row for the per-user limit;
- the fast decline;
- pools opened at startup;
- the log budget;
- the 503 mapping.

**What the AI found by testing, not by design:**
- the deploy-overlap connection exhaustion;
- Railway's log drops;
- the DNS-failure 500 during an outage;
- a flaky test, where the watcher's reads queued behind the burst;
- its own slips, which are worth being honest about: it forgot to bump the version for v0.2.0; it first assumed the Hobby plan gives 8 vCPUs, when it's 2; its burst script first counted client-side queueing as server latency; and its first log budget still let 1,578 lines drop.

Each slip was caught by a test or a measurement, and is recorded in the commit history.

## 8. What I'd do next

1. **Timed holds and a confirm step**, as sketched in section 3.
2. **Scale out properly:** more replicas, with Prometheus discovering and summing each one; PgBouncer in transaction mode; and a higher CPU limit on the app.
3. **A waiting room** (admission control) for very large on-sales, so the database only sees as many buyers as it can serve.
4. **Read path:** serve `GET /shows/{id}` from a short-TTL cache or a read replica. Bookings stay on the primary.
5. **Operations:**
   - a Grafana dashboard and hosted Prometheus with the alert rules;
   - public log search (for example Better Stack);
   - the outage test automated in CI;
   - moving `railway.json` to Railway's new config format before its 2026-12-01 deprecation.
6. **Per-user rate limiting**, returning `429`, to blunt bots before they reach the per-user limit.
