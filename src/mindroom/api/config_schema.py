"""Configuration JSON schema that drives dashboard config forms."""

from __future__ import annotations

from functools import cache
from typing import Any

from fastapi import APIRouter

from mindroom.config.main import Config
from mindroom.config.schema_hints import DashboardJsonSchema

router = APIRouter(prefix="/api/config", tags=["config"])


@cache
def _config_json_schema() -> dict[str, Any]:
    return Config.model_json_schema(schema_generator=DashboardJsonSchema)


@router.get("/schema")
async def get_config_schema() -> dict[str, Any]:
    """Return the annotated configuration JSON schema."""
    return _config_json_schema()
