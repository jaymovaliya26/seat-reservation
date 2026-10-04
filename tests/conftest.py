"""Shared fixtures.

Tests run against a real Postgres (the compose `db` service on localhost:5433), because the
behaviour we care about (row locks, constraints, races) only exists in a real database.
Each test session gets its own throwaway database. The app runs in a real uvicorn server, so
requests go over HTTP exactly as they do in production.
"""

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres"
)
TEST_ADMIN_KEY = "test-admin-key"
TEST_JWT_SECRET = "test-jwt-secret-with-enough-length-for-hs256"


def _with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{database}"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="session")
async def database_url() -> AsyncIterator[str]:
    name = f"test_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    yield _with_database(ADMIN_DSN, name)

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "jwt_secret": TEST_JWT_SECRET,
        "admin_api_key": TEST_ADMIN_KEY,
        "db_pool_max": 20,
        "log_level": "WARNING",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@dataclass
class LiveServer:
    app: FastAPI
    base_url: str


async def _boom() -> None:
    raise RuntimeError("deliberate failure for tests")


@pytest.fixture(scope="session")
async def live_server(database_url: str) -> AsyncIterator[LiveServer]:
    app = create_app(make_settings(database_url))
    app.add_api_route("/__test__/boom", _boom)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_config=None)
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(15):
        while not server.started:
            if task.done():
                task.result()  # surface the startup error
            await asyncio.sleep(0.05)

    yield LiveServer(app=app, base_url=f"http://127.0.0.1:{port}")

    server.should_exit = True
    await task


@pytest.fixture
async def client(live_server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=live_server.base_url, timeout=30) as http:
        yield http
