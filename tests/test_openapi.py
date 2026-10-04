"""The API documentation is part of the product: Authorize must work, errors must be described
in the real format, and the committed spec must match the code."""

import httpx

from scripts.export_openapi import SPEC_PATH, build_spec, render


def test_committed_spec_is_up_to_date() -> None:
    assert SPEC_PATH.read_text() == render(build_spec()), (
        "docs/openapi.json is out of date: run `make openapi` and commit it"
    )


def test_security_schemes_make_authorize_work() -> None:
    spec = build_spec()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": schemes["BearerAuth"]["description"],
    }
    assert schemes["AdminKey"]["type"] == "apiKey" and schemes["AdminKey"]["name"] == "X-Admin-Key"

    paths = spec["paths"]
    assert paths["/shows/{show_id}/reserve"]["post"]["security"] == [{"BearerAuth": []}]
    assert paths["/reservations/{reservation_id}/cancel"]["post"]["security"] == [
        {"BearerAuth": []}
    ]
    assert paths["/shows"]["post"]["security"] == [{"AdminKey": []}]
    assert paths["/admin/shows/{show_id}/reconcile"]["get"]["security"] == [{"AdminKey": []}]
    assert "security" not in paths["/shows/{show_id}"]["get"]  # reading a show is public


def test_errors_are_documented_in_the_real_format() -> None:
    reserve = build_spec()["paths"]["/shows/{show_id}/reserve"]["post"]["responses"]
    assert {"200", "201", "401", "404", "409", "422", "503"} <= set(reserve)
    conflict = reserve["409"]["content"]["application/json"]
    assert conflict["schema"] == {"$ref": "#/components/schemas/ErrorBody"}
    assert set(conflict["examples"]) == {
        "seat_taken",
        "per_user_limit",
        "idempotency_key_reused",
        "already_cancelled",
    }


async def test_swagger_ui_and_spec_are_served(client: httpx.AsyncClient) -> None:
    docs = await client.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text.lower()
    spec = await client.get("/openapi.json")
    assert spec.json()["info"]["title"] == "Seat Reservation API"
