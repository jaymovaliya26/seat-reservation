"""Domain errors and their HTTP mapping.

Every error response has the same shape, so clients (and the burst script) can branch on
`error.code` without parsing messages:

    {"error": {"code": "seat_taken", "message": "...", ...details}, "request_id": "..."}
"""

from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self, message: str, *, headers: dict[str, str] | None = None, **details: Any
    ) -> None:
        super().__init__(message)
        self.message = message
        self.headers = headers
        self.details = details


class InvalidRequest(AppError):
    status_code = 422
    code = "invalid_request"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"}, **details)


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class UnknownSeats(InvalidRequest):
    code = "unknown_seats"


class Conflict(AppError):
    """A clean, expected decline: the request was valid but the current state says no."""

    status_code = 409
    code = "conflict"


class SeatTaken(Conflict):
    code = "seat_taken"


class PerUserLimit(Conflict):
    code = "per_user_limit"


class IdempotencyKeyReused(Conflict):
    code = "idempotency_key_reused"


class AlreadyCancelled(Conflict):
    code = "already_cancelled"


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
    **details: Any,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {"code": code, "message": message, **details},
            "request_id": getattr(request.state, "request_id", None),
        },
        status_code=status_code,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # Lands on this request's access-log line, so declines can be counted by reason.
        structlog.contextvars.bind_contextvars(error_code=exc.code)
        return error_response(
            request, exc.status_code, exc.code, exc.message, exc.headers, **exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"field": ".".join(str(part) for part in error["loc"]), "problem": error["msg"]}
            for error in exc.errors()
        ]
        return error_response(
            request, 422, "invalid_request", "The request is not valid", problems=problems
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(request, exc.status_code, code, str(exc.detail), exc.headers)
