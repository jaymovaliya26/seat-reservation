"""Operator endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_pool
from app.auth import require_admin
from app.errors import NotFound
from app.models import ReconcileOut
from app.services import reconcile as reconcile_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/shows/{show_id}/reconcile")
async def reconcile(show_id: UUID, request: Request) -> ReconcileOut:
    """Cross-check seats, reservations and per-user counters for one show. `ok` is false if
    anything disagrees, with up to five examples per failing check."""
    result = await reconcile_service.reconcile(get_pool(request), show_id)
    if result is None:
        raise NotFound("Show not found", show_id=str(show_id))
    return result
