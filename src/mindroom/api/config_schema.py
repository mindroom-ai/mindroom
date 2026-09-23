"""Configuration JSON schema that drives dashboard config forms."""

from __future__ import annotations

from functools import cache
from typing import Any

from fastapi import APIRouter

from mindroom.config.main import dashboard_config_schema

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/schema")
async def get_config_schema() -> dict[str, Any]:
    """Return the annotated configuration JSON schema."""
    return _cached_config_schema()


@cache
def _cached_config_schema() -> dict[str, Any]:
    return dashboard_config_schema()
