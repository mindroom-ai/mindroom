"""Configuration JSON schema that drives dashboard config forms."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from mindroom.config.main import dashboard_config_schema

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/schema")
async def get_config_schema() -> dict[str, Any]:
    """Return the annotated configuration JSON schema."""
    return dashboard_config_schema()
