"""Reserving seats."""

from uuid import UUID

import structlog
from fastapi import APIRouter, Request

from app.api.deps import get_catalog, get_pool
from app.auth import CurrentUser
from app.errors import NotFound, UnknownSeats
from app.models import ReservationOut, ReserveRequest
from app.services import reservations

router = APIRouter(tags=["reservations"])


@router.post(
    "/shows/{show_id}/reserve",
    status_code=201,
    responses={409: {"description": "Seat already taken"}},
)
async def reserve(
    show_id: UUID, body: ReserveRequest, user_id: CurrentUser, request: Request
) -> ReservationOut:
    structlog.contextvars.bind_contextvars(user_id=user_id, show_id=str(show_id))

    show = await get_catalog(request).get(show_id)
    if show is None:
        raise NotFound("Show not found", show_id=str(show_id))
    unknown = sorted(set(body.seats) - show.seat_labels)
    if unknown:
        raise UnknownSeats("Seats do not exist in this show: " + ", ".join(unknown), seats=unknown)

    return await reservations.reserve(get_pool(request), show, user_id, body.seats)
