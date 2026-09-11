"""Read-only response activity for the bundled runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.response_activity import ResponseActivity
from mindroom.runtime_state import get_runtime_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/api/responses", tags=["responses"])


async def track_openai_request(request: Request) -> AsyncIterator[None]:
    """Keep an OpenAI request counted until its response body has finished."""
    state = config_lifecycle.app_state(request.app)
    state.active_openai_requests += 1
    try:
        yield
    finally:
        state.active_openai_requests -= 1


@router.get("/activity")
async def response_activity(request: Request) -> JSONResponse:
    """Read live counters on the runtime loop without scanning logs or storage."""
    state = config_lifecycle.app_state(request.app)
    gate = state.response_admission_gate
    snapshot = ResponseActivity(
        runtime_phase=get_runtime_state().phase,
        admission_paused=gate.closed if gate is not None else None,
        active_matrix_operations=gate.active_operation_count if gate is not None else None,
        active_openai_requests=state.active_openai_requests,
    )
    return JSONResponse(
        snapshot.model_dump(),
        status_code=503 if snapshot.status == "unavailable" else 200,
        headers={"Cache-Control": "no-store"},
    )
