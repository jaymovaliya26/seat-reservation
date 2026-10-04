import uuid

import httpx
import pytest

from tests.conftest import ADMIN_HEADERS, create_show


async def test_create_show_returns_every_seat_available(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/shows",
        json={"name": "friday-night", "seats": ["A1", "A2", "A3"], "price_paise": 25_000},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201
    show = response.json()
    uuid.UUID(show["id"])
    assert show["name"] == "friday-night"
    assert show["price_paise"] == 25_000
    assert show["per_user_limit"] == 4  # the default
    assert show["counts"] == {"total": 3, "available": 3, "held": 0, "confirmed": 0}
    assert show["seats"] == [
        {"label": "A1", "status": "available"},
        {"label": "A2", "status": "available"},
        {"label": "A3", "status": "available"},
    ]


async def test_get_show_matches_what_was_created(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2", "A10"], per_user_limit=2)
    response = await client.get(f"/shows/{show_id}")
    assert response.status_code == 200
    show = response.json()
    assert show["per_user_limit"] == 2
    assert show["counts"] == {"total": 3, "available": 3, "held": 0, "confirmed": 0}
    # Seats come back in the order they were given, not string order (A10 after A2).
    assert [seat["label"] for seat in show["seats"]] == ["A1", "A2", "A10"]


async def test_large_hall(client: httpx.AsyncClient) -> None:
    seats = [f"{row}{n}" for row in "ABCDEFGHIJKLMNOPQRST" for n in range(1, 501)]
    show_id = await create_show(client, seats)
    show = (await client.get(f"/shows/{show_id}")).json()
    assert show["counts"]["total"] == show["counts"]["available"] == 10_000


async def test_unknown_show_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/shows/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_malformed_show_id_is_422(client: httpx.AsyncClient) -> None:
    response = await client.get("/shows/not-a-uuid")
    assert response.status_code == 422


async def test_create_show_requires_the_admin_key(client: httpx.AsyncClient) -> None:
    body = {"name": "x", "seats": ["A1"], "price_paise": 100}
    missing = await client.post("/shows", json=body)
    wrong = await client.post("/shows", json=body, headers={"X-Admin-Key": "guess"})
    assert missing.status_code == 401
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    ("change", "problem_field"),
    [
        ({"price_paise": 250.5}, "body.price_paise"),  # floats are never money
        ({"price_paise": 25000.0}, "body.price_paise"),
        ({"price_paise": "25000"}, "body.price_paise"),
        ({"price_paise": 0}, "body.price_paise"),
        ({"price_paise": -100}, "body.price_paise"),
        ({"seats": []}, "body.seats"),
        ({"seats": ["A1", "A2", "A1"]}, "body.seats"),
        ({"seats": ["A 1"]}, "body.seats.0"),
        ({"seats": ["A1", ""]}, "body.seats.1"),
        ({"name": "   "}, "body.name"),
        ({"per_user_limit": 0}, "body.per_user_limit"),
    ],
)
async def test_create_show_validation(
    client: httpx.AsyncClient, change: dict[str, object], problem_field: str
) -> None:
    body = {"name": "friday-night", "seats": ["A1", "A2"], "price_paise": 25_000, **change}
    response = await client.post("/shows", json=body, headers=ADMIN_HEADERS)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert problem_field in [problem["field"] for problem in error["problems"]]


async def test_duplicate_seats_are_named(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/shows",
        json={"name": "x", "seats": ["A1", "B2", "A1", "B2"], "price_paise": 100},
        headers=ADMIN_HEADERS,
    )
    assert "duplicate seats: A1, B2" in response.json()["error"]["problems"][0]["problem"]
