"""Reserving, viewing and cancelling seats."""

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, Request, Response

from app.api.deps import get_catalog, get_pool
from app.auth import CurrentUser
from app.errors import AppError, InvalidRequest, NotFound, UnknownSeats
from app.models import IdempotencyKey, ReservationOut, ReserveRequest
from app.observability import metrics
from app.services import reservations

router = APIRouter(tags=["reservations"])
log = structlog.get_logger(component="reservations")


@router.post(
    "/shows/{show_id}/reserve",
    status_code=201,
    responses={
        200: {"description": "Replay of an earlier request with the same idempotency key"},
        409: {"description": "seat_taken, per_user_limit or idempotency_key_reused"},
    },
)
async def reserve(
    show_id: UUID,
    body: ReserveRequest,
    user_id: CurrentUser,
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[
        IdempotencyKey | None, Header(alias="Idempotency-Key")
    ] = None,
) -> ReservationOut:
    structlog.contextvars.bind_contextvars(user_id=user_id, show_id=str(show_id))
    if (body.model_extra or {}).get("user_id") not in (None, user_id):
        log.warning("body_user_id_ignored")

    try:
        key = _idempotency_key(idempotency_key_header, body.idempotency_key)
        show = await get_catalog(request).get(show_id)
        if show is None:
            raise NotFound("Show not found", show_id=str(show_id))
        unknown = sorted(set(body.seats) - show.seat_labels)
        if unknown:
            raise UnknownSeats(
                "Seats do not exist in this show: " + ", ".join(unknown), seats=unknown
            )
        result = await reservations.reserve(get_pool(request), show, user_id, body.seats, key)
    except AppError as exc:
        metrics.RESERVATIONS_DECLINED.labels(reason=exc.code).inc()
        raise

    if result.replayed:
        # Counted as a decline: the request was answered, but nothing new was booked.
        metrics.RESERVATIONS_DECLINED.labels(reason="idempotent_replay").inc()
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
        structlog.contextvars.bind_contextvars(outcome="replayed")
    else:
        metrics.RESERVATIONS_CONFIRMED.inc()
        metrics.SEATS_SOLD.inc(len(result.reservation.seats))
        structlog.contextvars.bind_contextvars(
            outcome="confirmed", reservation_id=str(result.reservation.reservation_id)
        )
    return result.reservation


@router.get("/reservations/{reservation_id}")
async def get_reservation(
    reservation_id: UUID, user_id: CurrentUser, request: Request
) -> ReservationOut:
    """Your own reservation. Someone else's is a 404, so its existence is never revealed."""
    reservation = await reservations.get_reservation(get_pool(request), reservation_id, user_id)
    if reservation is None:
        raise NotFound("Reservation not found", reservation_id=str(reservation_id))
    return reservation


@router.post(
    "/reservations/{reservation_id}/cancel",
    responses={
        404: {"description": "No such reservation, or not yours"},
        409: {"description": "already_cancelled"},
    },
)
async def cancel(reservation_id: UUID, user_id: CurrentUser, request: Request) -> ReservationOut:
    """Cancel your own reservation. Its seats become available to anyone at once."""
    structlog.contextvars.bind_contextvars(user_id=user_id, reservation_id=str(reservation_id))
    result = await reservations.cancel(get_pool(request), reservation_id, user_id)
    metrics.RESERVATIONS_CANCELLED.inc()
    metrics.SEATS_RELEASED.inc(len(result.seats))
    structlog.contextvars.bind_contextvars(outcome="cancelled")
    return result


def _idempotency_key(header: str | None, body: str | None) -> str | None:
    """The key may come in the Idempotency-Key header or the body; both is fine if they agree.

    Without a key the request still works, but a retry of it would book again. Rejecting it
    would turn a client's missing header into a failed purchase, so we accept it and log it.
    """
    if header is not None and body is not None and header != body:
        raise InvalidRequest("Idempotency-Key header and idempotency_key field disagree")
    key = header or body
    if key is None:
        log.info("idempotency_key_missing")
    return key
