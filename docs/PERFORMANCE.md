# Performance

How the service behaves under the on-sale burst, what measuring it uncovered, and what changed because of it.

## Setup

- **Live:** Railway, Singapore. The app and Postgres are separate services, each limited to **2 vCPU and 1 GB** on this plan. One replica runs 4 Gunicorn workers with 8 database connections each.
- **Load:** `./scripts/burst.sh` sends 20,000 reserve requests, 500 at a time, from a laptop in India. The mix is hot seats, overlapping multi-seat requests, racing retries, per-user floods, spoofed identities and random seats. It then runs 25 correctness checks.
- **Local benchmark:** the same burst against the Docker image, with the app limited to 2 CPUs like Railway and Postgres serving only that app. CPU time comes from each container's cgroup counters.

## Results

Every run had **0 × 5xx and passed all 25 checks**, except the one run marked otherwise below.

**Live, from India to Singapore:**

| Build | Throughput | p50 | p99 | Max |
|---|---|---|---|---|
| v0.4 | 825 req/s | 551 ms | 1,726 ms | 5,568 ms |
| v0.5 (fast decline, connection fix) | 1,130 req/s | 344 ms | 1,314 ms | 2,805 ms |

Railway's own server-side measurement for the v0.5 run was p50 245 ms and p99 1,206 ms. The rest of the client-side latency is the network and TLS between India and Singapore.

**Local, app limited to 2 CPUs:**

| Build | Throughput | p50 | p99 | App CPU / request | Postgres CPU / request |
|---|---|---|---|---|---|
| v0.4 | 3,679 req/s | 111 ms | 459 ms | 0.60 ms | 0.52 ms |
| v0.4, a second instance on the same Postgres | 1,379 req/s | 190 ms | 2,377 ms | | **7,155 × 500** |
| v0.5 fast decline | 4,652 req/s | 91 ms | 382 ms | 0.49 ms | 0.29 ms |
| v0.5, a second instance on the same Postgres | 4,506 req/s | 93 ms | 391 ms | 0.52 ms | 0.29 ms |
| v0.5 with request logs off (for comparison) | 4,904 req/s | | | 0.46 ms | 0.30 ms |
| v0.5 with the log budget | 4,064 req/s | 95 ms | 466 ms | 0.55 ms | varies run to run |

Run-to-run noise is roughly ±15%. The comparisons that matter come from back-to-back runs.

## What measuring found, and what changed

**1. Losing buyers were expensive.** About 77% of a burst's requests lose. Each loser still paid for a whole write transaction: insert the reservation, update the holdings, try the claim, roll back.

*Change:* a **fast decline**. One indexed read answers `409` when another user already holds a requested seat. It can't double-sell, because a stale read can only err in one direction. A seat seen as taken really was taken at that moment. A seat seen as free still goes through the locked claim. Postgres CPU per request fell 44%, and throughput rose 26%.

**2. A deploy could cause 500s.** Each instance opened up to 4 × (15 + 2) = 68 connections, lazily, in the middle of a burst. A Railway deploy runs the old and new instance side by side for 15 seconds: 136 connections, against Postgres's limit of 100. The benchmark reproduced it as 7,155 × `TooManyConnectionsError`.

*Change:* pools of 8 per worker, all opened at startup, plus one readiness connection. That's 36 per instance, and 72 during an overlap. A refused connection can now only happen at startup, where it is retried. "Can't connect right now" errors are also retried inside a request, because they happen before any statement runs.

**3. Railway dropped most of the logs.** Railway keeps at most 500 log lines per second per replica. During the live burst it dropped about 28,800 lines, at random.

*Change:* a per-worker **log budget**. Server errors are always logged; skipped lines are summarised once a second and counted in `log_lines_suppressed_total`. At 100 lines/s per worker, the live burst lost 1,578 lines instead of 28,800. Two problems remained. Each worker's one-second window started at an arbitrary moment, so a worker could write up to twice its cap within one real second. And Railway dropped lines even in seconds where only about 400 were stored. The windows are now whole wall-clock seconds and the cap is 50 per worker (200/s in total). Locally the same burst writes 2,734 lines instead of 19,954, and the app spends about 10% less CPU per request.

**4. Where the time goes now.** Live, the app is the bottleneck: it's CPU-bound at its 2 vCPU limit, while Postgres has headroom. Locally the same image does about 4,000 req/s, which suggests Railway's vCPUs are several times slower than a laptop core.

## Why one replica

A second replica would double the capacity. But a request to `/metrics` reaches just one replica, so `curl /metrics` would show only part of the counts, and the brief requires metrics that reconcile with what clients observed. Running several replicas properly needs a Prometheus that scrapes each replica and sums them, plus PgBouncer to keep Postgres connections flat. Both are listed under next steps.

## Next levers, cheapest first

1. **Raise the service's CPU limit** in Railway's settings. Billing is by usage, so idle capacity costs nothing.
2. **More replicas**, with Prometheus scraping each one and PgBouncer (transaction mode) in front of Postgres.
3. **Less CPU per request:** sign tokens with a cheaper scheme, skip response validation on hot paths, render the request log line directly.
4. **For mega on-sales:** a waiting room for admission control in front of the API, so the database only ever sees as many buyers as it can serve.

## Reproduce

```bash
./scripts/burst.sh https://seat-reservation-jm.up.railway.app "$ADMIN_KEY"    # live
make up && make burst                                                          # local
railway metrics --all --since 10m                                              # live CPU and memory
```
