"""Per-request context: a request ID on every response and a structured log line per request
(within the per-worker log budget)."""

import re
import time
import uuid

import orjson
import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import metrics
from app.observability.log_budget import LogBudget

REQUEST_ID_HEADER = b"x-request-id"
_VALID_REQUEST_ID = re.compile(rb"^[A-Za-z0-9._:-]{1,128}$")
# Probes that would drown the useful lines under load.
_QUIET_PATHS = frozenset({"/healthz", "/metrics"})

log = structlog.get_logger(component="http")


class RequestContextMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware) so it adds almost nothing per request.

    It also turns any unhandled exception into a JSON 500 carrying the request ID. Starlette's
    own error handler sits outside user middleware and would drop that header.
    """

    def __init__(self, app: ASGIApp, log_budget: LogBudget | None = None) -> None:
        self.app = app
        self.log_budget = log_budget or LogBudget(per_second=0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        response_started = False
        start = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER, request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            log.exception("unhandled_error", method=scope["method"], path=scope["path"])
            if response_started:
                raise
            status = 500
            await _send_internal_error(send, request_id)
        finally:
            elapsed = time.perf_counter() - start
            # The route template ("/shows/{show_id}/reserve"), never the raw path, so metric
            # labels stay few no matter how many shows exist.
            route = getattr(scope.get("route"), "path", "unmatched")
            metrics.HTTP_REQUESTS.labels(scope["method"], route, str(status)).inc()
            metrics.HTTP_DURATION.labels(scope["method"], route).observe(elapsed)
            if scope["path"] not in _QUIET_PATHS and self.log_budget.allow(status):
                log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == REQUEST_ID_HEADER:
            return value.decode("ascii") if _VALID_REQUEST_ID.match(value) else None
    return None


async def _send_internal_error(send: Send, request_id: str) -> None:
    body = orjson.dumps(
        {
            "error": {"code": "internal_error", "message": "Unexpected server error"},
            "request_id": request_id,
        }
    )
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (REQUEST_ID_HEADER, request_id.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
