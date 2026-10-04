"""Prometheus scrape endpoint."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app import __version__
from app.api.deps import get_pool
from app.observability import metrics

router = APIRouter(tags=["operations"])


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=PlainTextResponse,
    responses={200: {"description": "Prometheus text format, summed across all workers"}},
)
async def scrape(request: Request) -> Response:
    body = await metrics.render(get_pool(request), __version__)
    return Response(body, media_type=metrics.CONTENT_TYPE, headers={"Cache-Control": "no-store"})
