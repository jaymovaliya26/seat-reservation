import time

import httpx
import jwt
import pytest

from tests.conftest import TEST_JWT_SECRET, bearer_for


async def test_token_identifies_the_user(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/token", json={"user_id": "alice"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user_id"] == "alice"
    assert body["expires_in"] == 86_400

    whoami = await client.get(
        "/__test__/whoami", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert whoami.json() == {"user_id": "alice"}


@pytest.mark.parametrize("user_id", ["", " alice", "a" * 65, "alice; drop", "../admin"])
async def test_token_rejects_malformed_user_ids(client: httpx.AsyncClient, user_id: str) -> None:
    response = await client.post("/auth/token", json={"user_id": user_id})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_missing_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/__test__/whoami")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    ["Basic YWxpY2U6cHc=", "Bearer", "Bearer not-a-jwt", "Token abc"],
)
async def test_malformed_authorization_is_401(client: httpx.AsyncClient, header: str) -> None:
    response = await client.get("/__test__/whoami", headers={"Authorization": header})
    assert response.status_code == 401


def _forge(claims: dict[str, object], secret: str = TEST_JWT_SECRET, alg: str = "HS256") -> str:
    return jwt.encode(claims, secret, algorithm=alg)


async def test_token_signed_with_another_secret_is_401(client: httpx.AsyncClient) -> None:
    now = int(time.time())
    token = _forge({"sub": "mallory", "iat": now, "exp": now + 60}, secret="x" * 40)
    response = await client.get("/__test__/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_expired_token_is_401(client: httpx.AsyncClient) -> None:
    now = int(time.time())
    token = _forge({"sub": "alice", "iat": now - 120, "exp": now - 60})
    response = await client.get("/__test__/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_unsigned_alg_none_token_is_401(client: httpx.AsyncClient) -> None:
    now = int(time.time())
    token = jwt.encode({"sub": "alice", "iat": now, "exp": now + 60}, key=None, algorithm="none")
    response = await client.get("/__test__/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_token_without_expiry_is_401(client: httpx.AsyncClient) -> None:
    token = _forge({"sub": "alice", "iat": int(time.time())})
    response = await client.get("/__test__/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_each_user_gets_their_own_identity(client: httpx.AsyncClient) -> None:
    for user in ("u-1", "u-2", "carol@example.com"):
        response = await client.get("/__test__/whoami", headers=await bearer_for(client, user))
        assert response.json() == {"user_id": user}
