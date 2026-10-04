"""Who is calling.

A user's identity comes only from a signed token. Request bodies never carry identity, so a
`user_id` field in a body is simply ignored. Admin routes take a static key in X-Admin-Key.
"""

import hmac
import re
import time
from typing import Annotated, cast

import jwt
from fastapi import Depends, Request

from app.config import Settings
from app.errors import Forbidden, Unauthorized

USER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$"
_USER_ID = re.compile(USER_ID_PATTERN)
_ALGORITHM = "HS256"


def issue_token(user_id: str, settings: Settings, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    claims = {"sub": user_id, "iat": issued_at, "exp": issued_at + settings.jwt_ttl_s}
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def verify_token(token: str, settings: Settings) -> str:
    """Return the token's user ID, or raise jwt.InvalidTokenError.

    Pinning the algorithm list blocks the classic `alg: none` and algorithm-confusion attacks.
    """
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[_ALGORITHM],
        options={"require": ["sub", "iat", "exp"]},
    )
    user_id = claims["sub"]
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise jwt.InvalidTokenError("sub is not a valid user id")
    return user_id


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def current_user(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise Unauthorized("Missing bearer token")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("Authorization header must be 'Bearer <token>'")
    try:
        return verify_token(token.strip(), _settings(request))
    except jwt.InvalidTokenError:
        raise Unauthorized("Token is invalid or expired") from None


async def require_admin(request: Request) -> None:
    given = request.headers.get("x-admin-key")
    if given is None:
        raise Unauthorized("Missing X-Admin-Key header")
    expected = _settings(request).admin_api_key.get_secret_value()
    # Constant-time comparison, so response timing reveals nothing about the key.
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise Forbidden("Invalid admin key")


CurrentUser = Annotated[str, Depends(current_user)]
