# Seat Reservation

A JSON API that sells assigned seats for a show and guarantees each seat is sold exactly once, even when thousands of buyers try to book the same seat in the same second.

> Work in progress. The release plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

## Stack

Python 3.12 · FastAPI · asyncpg · PostgreSQL 16 · Gunicorn + Uvicorn · Docker · Railway

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
cp .env.example .env
```
