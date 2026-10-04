import re

import httpx
import pytest

from app.db import Database
from tests.conftest import LiveServer, make_settings


async def test_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_when_database_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}
    assert response.headers["cache-control"] == "no-store"


async def test_readiness_fails_closed_when_database_is_down(
    client: httpx.AsyncClient, live_server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def database_down() -> bool:
        return False

    monkeypatch.setattr(live_server.app.state.db, "ping", database_down)
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "down"}}


async def test_ping_is_false_when_never_connected(database_url: str) -> None:
    assert await Database(make_settings(database_url)).ping() is False


async def test_connect_gives_up_after_startup_timeout() -> None:
    settings = make_settings(
        "postgresql://postgres:postgres@127.0.0.1:1/nothing", db_startup_timeout_s=0.5
    )
    with pytest.raises(OSError):
        await Database(settings).connect()


async def test_request_id_is_generated(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_request_id_is_propagated(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "burst-42.a"})
    assert response.headers["x-request-id"] == "burst-42.a"


async def test_malformed_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "not valid; drop table"})
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_unhandled_error_is_json_500_with_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/__test__/boom", headers={"X-Request-ID": "trace-me"})
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-me"
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Unexpected server error"},
        "request_id": "trace-me",
    }
