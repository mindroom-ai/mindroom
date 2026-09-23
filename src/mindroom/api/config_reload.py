"""Authenticated confirmation of runtime config application."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_operator_key
from mindroom.config_reload import ConfigReloadStatus
from mindroom.runtime_state import get_runtime_state

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/reload-status")
async def config_reload_status(request: Request, authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
    """Read the reload owner's receipt; API config caches do not prove application."""
    require_operator_key(request, authorization)
    provider = config_lifecycle.app_state(request.app).config_reload_status
    status = provider() if provider is not None and get_runtime_state().phase == "ready" else ConfigReloadStatus()
    return JSONResponse(
        status.model_dump(),
        status_code=503 if status.status == "unavailable" else 200,
        headers={"Cache-Control": "no-store"},
    )
