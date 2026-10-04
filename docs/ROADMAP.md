# Roadmap

Ship a correct MVP to a public URL first, then add one capability per release. Every release is small commits on `main`, green CI, an automatic deploy to Railway, a smoke test against the live URL, a git tag and a `CHANGELOG.md` entry.

## Design in one paragraph

Postgres is the only place a booking decision is made. Each seat is one row with one status, so a double sale cannot be stored and `available + held + confirmed == total_seats` holds by construction. A reservation is one transaction that takes locks in a fixed global order (idempotency key, then the user's holdings row, then seats sorted by label) and claims seats with a statement guarded on `status = 'available'`. A request that waited for a seat lock re-checks that condition against the committed row, so exactly one buyer wins and everyone else gets a clean 409.

## Releases

| Release | Theme | Done when | Status |
|---|---|---|---|
| v0.1.0 (MVP) | Sell seats correctly, on a public URL: Docker, CI, migrations, health and readiness, JWT auth, create and read shows, all-or-nothing reserve | 500 parallel buyers for one seat give one 201 and 499 × 409; overlapping multi-seat requests never deadlock; live `/readyz` returns 200 | shipped |
| v0.2.0 | Safe retries and fair limits: idempotency keys, per-user seat limit, transaction retry, timeouts | 50 parallel calls with one key create one reservation; 10 parallel calls on a limit-4 show leave at most 4 seats | shipped |
| v0.3.0 | Release seats: owner-only cancel, reservation lookup, reconcile endpoint, randomized test | A cancel never frees a seat sold to someone else; reconcile passes after thousands of random operations | shipped |
| v0.4.0 | Observability: Prometheus metrics, per-request JSON logs, pool and retry metrics, alert rules | Metrics match API outcomes exactly; `seats_available` matches `GET /shows/{id}` | shipped |
| v0.5.0 | Ready for the stampede: one-command burst script, fast decline path, tuning, chaos test | Burst against the live URL: zero 5xx, one winner per hot seat, reconcile passes | shipped |
| v1.0.0 | Submission: fresh-clone and cold-start checks, README, WRITEUP.md, log recording | Someone with only the README can build, run and burst it | this release |

Stretch, if time allows: timed holds with expiry, Prometheus and Grafana on Railway, PgBouncer, per-user rate limiting.
