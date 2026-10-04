"""Test-user tokens.

In production, tokens would come from the identity provider. This endpoint stands in for it so
load tests and reviewers can act as many different users. It signs whatever user_id it is
given; what it guarantees is that every other endpoint trusts only the signed token.
"""

from fastapi import APIRouter, Request

from app.api.deps import get_settings
from app.auth import issue_token
from app.models import TokenRequest, TokenResponse

router = APIRouter(tags=["auth"])


@router.post("/auth/token")
async def create_token(body: TokenRequest, request: Request) -> TokenResponse:
    settings = get_settings(request)
    return TokenResponse(
        access_token=issue_token(body.user_id, settings),
        expires_in=settings.jwt_ttl_s,
        user_id=body.user_id,
    )
