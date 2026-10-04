"""When the database can't answer, the service refuses clearly instead of guessing."""

import socket

import asyncpg
import httpx
import pytest

from app.services import reservations
from tests.conftest import create_show, user_headers


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
            "database_unavailable",
        ),
        (
            asyncpg.CannotConnectNowError("the database system is starting up"),
            "database_unavailable",
        ),
        (ConnectionRefusedError("connection refused"), "database_unavailable"),
        # What a real outage raised when Postgres's hostname stopped resolving.
        (socket.gaierror(-2, "Name or service not known"), "database_unavailable"),
        (TimeoutError("connect timed out"), "database_unavailable"),
        (
            asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
            "database_busy",
        ),
        (asyncpg.LockNotAvailableError("canceling statement due to lock timeout"), "database_busy"),
    ],
)
async def test_database_failures_are_503_with_retry_after(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    show_id = await create_show(client, ["A1"])

    async def broken(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(reservations, "reserve", broken)
    response = await client.post(
        f"/shows/{show_id}/reserve", json={"seats": ["A1"]}, headers=user_headers("alice")
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    body = response.json()
    assert body["error"]["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
