"""Prometheus scrape endpoint."""

from fastapi import APIRouter, Request, Response

from app import __version__
from app.api.deps import get_pool
from app.observability import metrics

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def scrape(request: Request) -> Response:
    body = await metrics.render(get_pool(request), __version__)
    return Response(body, media_type=metrics.CONTENT_TYPE, headers={"Cache-Control": "no-store"})
