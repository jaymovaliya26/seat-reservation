"""Liveness and readiness probes."""

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

router = APIRouter(tags=["operations"])

CheckState = Literal["ok", "down"]


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, CheckState]


@router.get("/healthz", summary="Liveness")
async def healthz() -> Liveness:
    """The process is up and serving. Never touches the database."""
    return Liveness()


@router.get(
    "/readyz",
    summary="Readiness",
    responses={503: {"model": Readiness, "description": "Postgres unreachable"}},
)
async def readyz(request: Request, response: Response) -> Readiness:
    """Postgres answers within the timeout. Fails closed with 503 otherwise."""
    database_ok: bool = await request.app.state.db.ping()
    checks: dict[str, CheckState] = {"database": "ok" if database_ok else "down"}

    response.headers["Cache-Control"] = "no-store"
    if all(state == "ok" for state in checks.values()):
        return Readiness(status="ready", checks=checks)
    response.status_code = 503
    return Readiness(status="unavailable", checks=checks)
