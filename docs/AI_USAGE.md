# AI usage log

The assignment allows and expects AI tools. This log records, per step, what I asked the AI to do, what it produced, and what I decided myself. It feeds the AI section of `WRITEUP.md`.

Tool: Claude Code (Claude Opus 5.5).

| Date | Step | Directed / produced by AI | Decided by me |
|---|---|---|---|
| 2026-10-04 | Planning | Summarised the assignment, listed prerequisites, proposed a Postgres-only design (conditional seat claim with ordered row locks, unique constraint for idempotency, holdings row for the per-user limit) and an MVP-first release plan | Stack: Python + FastAPI. Hosting: Railway. Release model: confirm on reserve plus owner-only cancel. Ship an MVP first, then incremental releases. Working style: AI implements in small commits, I review each milestone before moving on |
| 2026-10-04 | Setup | Installed the Railway CLI, created the uv project, added dependencies, wrote lint, type-check and test settings, generated local secrets into `.env` | Repository name and GitHub account |
| 2026-10-04 | v0.1 skeleton, container, migrations | Wrote the service skeleton (pools with startup retry, health probes, request-ID middleware, JSON logs), the Dockerfile and Gunicorn config, the advisory-locked migration runner and the schema; tested a database outage and graceful shutdown by hand | Reviewed this milestone before it was pushed; asked for commit identity to use my personal email |
| 2026-10-04 | v0.1 auth, shows, reserve | Wrote JWT auth, the admin key, show endpoints, the single-statement reserve transaction, the per-worker show catalog, and the concurrency tests; measured throughput locally and on Railway | Asked for a live progress tracker alongside the build |
| 2026-10-04 | v0.1 CI and deploy | Wrote the CI workflow and Railway config; created the Railway project, Postgres and app service via the CLI; moved both from the default US West region to Singapore; renamed the public domain; found and fixed a flaky test; aligned local Postgres to Railway's version 18 | Approved each push and the creation of the Railway deployment |
| 2026-10-04 | v0.2 idempotency and limits | Wrote the expand-only migrations and an upgrade test from v0.1 data, the retry helper, idempotent reserve with per-user holdings, and their tests; split the work into commits that each pass on their own; measured the throughput cost | Approved pushing v0.1.0 before v0.2 started |
| 2026-10-04 | v0.3 cancel and reconcile | Wrote cancel, reservation lookup, the reconcile audit and their tests (including corruption and randomized-storm tests); found that the version string had not been bumped for v0.2.0 and added a test for it; found a duplicate Railway project failing on every push, deleted it and disconnected it from the repo | Approved deleting the duplicate Railway project |
