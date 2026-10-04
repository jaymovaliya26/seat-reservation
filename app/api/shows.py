"""Shows: create (admin) and read the seat map."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_pool, get_settings
from app.auth import require_admin
from app.errors import NotFound
from app.models import CreateShowRequest, ShowOut
from app.services import shows

router = APIRouter(prefix="/shows", tags=["shows"])


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
async def create_show(body: CreateShowRequest, request: Request) -> ShowOut:
    return await shows.create_show(
        get_pool(request),
        name=body.name,
        seats=body.seats,
        price_paise=body.price_paise,
        per_user_limit=body.per_user_limit or get_settings(request).default_per_user_limit,
    )


@router.get("/{show_id}")
async def get_show(show_id: UUID, request: Request) -> ShowOut:
    show = await shows.get_show(get_pool(request), show_id)
    if show is None:
        raise NotFound("Show not found", show_id=str(show_id))
    return show
