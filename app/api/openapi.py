"""What the API documentation (Swagger UI at /docs, ReDoc at /redoc) says beyond the routes.

Error responses are documented with the real error format and real examples, so a reader can
see exactly what a decline looks like before sending a request.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

DESCRIPTION = """
Sells assigned seats for a show and guarantees each seat is sold exactly once, even when
thousands of buyers want the same seat in the same second.

**Try it from this page**

1. `POST /auth/token` with `{"user_id": "alice"}`, then copy `access_token`.
2. Click **Authorize**: paste the token under **BearerAuth**, and the admin key under
   **AdminKey** (only needed to create shows and run the audit).
3. `POST /shows` to create a show, then `POST /shows/{show_id}/reserve`.
4. `GET /shows/{show_id}` shows every seat: `available + held + confirmed == total` always holds.

**Rules worth knowing**

- Identity comes only from the token; a `user_id` in a request body is ignored.
- Money is integer paise; a float or string price is rejected.
- A multi-seat request is all-or-nothing.
- The same idempotency key returns the same reservation (`200` replay); the same key with
  other seats is `409`.
- Every error has the same shape, `{"error": {"code", "message", ...}, "request_id"}`.
  Declines are `409`, never `5xx`. If the database is unreachable: `503` with `Retry-After`.

Source, write-up and the one-command burst:
[github.com/jaymovaliya26/seat-reservation](https://github.com/jaymovaliya26/seat-reservation)
"""

TAGS = [
    {"name": "auth", "description": "Test-user tokens. Stands in for an identity provider."},
    {"name": "shows", "description": "Create a show (admin) and read its seat map."},
    {"name": "reservations", "description": "Reserve, view and cancel seats."},
    {"name": "admin", "description": "Operator endpoints."},
    {"name": "operations", "description": "Health, readiness and Prometheus metrics."},
]

SWAGGER_UI_PARAMETERS = {
    "persistAuthorization": True,  # keep the token across page reloads
    "displayRequestDuration": True,
    "tryItOutEnabled": True,
    "defaultModelsExpandDepth": 0,
}


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="allow")  # decline details such as seats, limit, held

    code: str
    message: str


class ErrorBody(BaseModel):
    error: ErrorInfo
    request_id: str | None


def _example(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "value": {
            "error": {"code": code, "message": message, **details},
            "request_id": "9f2c4a1e0b7d4c55a3e1f0d2b6c8e4a7",
        }
    }


_ERRORS: dict[int, dict[str, Any]] = {
    401: {
        "description": "Missing or invalid token (or missing admin key)",
        "examples": {
            "missing token": _example("unauthorized", "Missing bearer token"),
            "expired token": _example("unauthorized", "Token is invalid or expired"),
        },
    },
    403: {
        "description": "Wrong admin key",
        "examples": {"wrong key": _example("forbidden", "Invalid admin key")},
    },
    404: {
        "description": "Not found (including another user's reservation)",
        "examples": {"not found": _example("not_found", "Show not found")},
    },
    409: {
        "description": "A clean decline: the request was valid, but the current state says no",
        "examples": {
            "seat_taken": _example("seat_taken", "Seats already taken: A12", seats=["A12"]),
            "per_user_limit": _example(
                "per_user_limit",
                "At most 4 seats per user for this show",
                limit=4,
                held=3,
                requested=2,
            ),
            "idempotency_key_reused": _example(
                "idempotency_key_reused",
                "This idempotency key was already used for a different request",
                reservation_id="0b6f2f6e-6a1e-4b8e-9d3a-2c7f1e5a9b10",
            ),
            "already_cancelled": _example("already_cancelled", "Reservation is already cancelled"),
        },
    },
    422: {
        "description": "The request is malformed",
        "examples": {
            "invalid body": _example(
                "invalid_request",
                "The request is not valid",
                problems=[
                    {"field": "body.price_paise", "problem": "Input should be a valid integer"}
                ],
            ),
            "unknown seats": _example(
                "unknown_seats", "Seats do not exist in this show: Z9", seats=["Z9"]
            ),
        },
    },
    503: {
        "description": "The database is unreachable or overloaded; retry with the same key "
        "after `Retry-After` seconds",
        "examples": {
            "unavailable": _example(
                "database_unavailable",
                "The database is unreachable; retry shortly with the same idempotency key",
            ),
            "busy": _example(
                "database_busy",
                "The database is overloaded; retry shortly with the same idempotency key",
            ),
        },
    },
}


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` entries for these error statuses, in the real error format."""
    return {
        status: {
            "model": ErrorBody,
            "description": _ERRORS[status]["description"],
            "content": {"application/json": {"examples": _ERRORS[status]["examples"]}},
        }
        for status in statuses
    }
