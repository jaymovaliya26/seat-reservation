# AI usage log

The assignment allows and expects AI tools. This log records, per step, what I asked the AI to do, what it produced, and what I decided myself. It feeds the AI section of `WRITEUP.md`.

Tool: Claude Code (Claude Opus 5.5).

| Date | Step | Directed / produced by AI | Decided by me |
|---|---|---|---|
| 2026-10-04 | Planning | Summarised the assignment, listed prerequisites, proposed a Postgres-only design (conditional seat claim with ordered row locks, unique constraint for idempotency, holdings row for the per-user limit) and an MVP-first release plan | Stack: Python + FastAPI. Hosting: Railway. Release model: confirm on reserve plus owner-only cancel. Ship an MVP first, then incremental releases. Working style: AI implements in small commits, I review each milestone before moving on |
| 2026-10-04 | Setup | Installed the Railway CLI, created the uv project, added dependencies, wrote lint, type-check and test settings, generated local secrets into `.env` | Repository name and GitHub account |
