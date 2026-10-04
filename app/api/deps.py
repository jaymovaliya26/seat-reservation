"""Shared route dependencies."""

from typing import cast

import asyncpg
from fastapi import Request

from app.config import Settings


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db.pool


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
