"""Shared route dependencies."""

from typing import cast

import asyncpg
from fastapi import Request

from app.config import Settings
from app.services.catalog import ShowCatalog


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db.pool


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_catalog(request: Request) -> ShowCatalog:
    return cast(ShowCatalog, request.app.state.catalog)
