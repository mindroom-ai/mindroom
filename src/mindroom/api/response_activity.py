"""Read-only response activity for the bundled runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_operator_key
from mindroom.response_activity import ActiveResponseInfo, DetailedResponseActivity, ResponseActivity, ResponseIdentity
from mindroom.runtime_state import get_runtime_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/api/responses", tags=["responses"])


async def track_openai_request(request: Request) -> AsyncIterator[ResponseIdentity]:
    """Observe one HTTP request through its normal response-body lifecycle."""
    responses = config_lifecycle.app_state(request.app).openai_responses
    identity = ResponseIdentity()
    responses.add(identity)
    try:
        yield identity
    finally:
        responses.remove(identity)


async def _response_activity_snapshot(request: Request) -> DetailedResponseActivity:
    """Capture every live source, reading in-memory counters after the off-loop script read."""
    state = config_lifecycle.app_state(request.app)
    script_runs = await state.active_script_runs() if state.active_script_runs is not None else None
    interruptible_script_runs: int | None = None
    recoverable_script_runs: int | None = None
    if script_runs is not None:
        recoverable_script_runs = sum(run.recoverable for run in script_runs)
        interruptible_script_runs = len(script_runs) - recoverable_script_runs
    calls = state.active_calls() if state.active_calls is not None else None
    gate = state.response_admission_gate
    responses = [
        ActiveResponseInfo(channel=channel, responder=identity.responder, requester_id=identity.requester_id)
        for channel, identities in (
            ("matrix", gate.response_identities if gate is not None else ()),
            ("openai", state.openai_responses),
            ("call", calls or ()),
        )
        for identity in identities
    ]
    return DetailedResponseActivity(
        runtime_phase=get_runtime_state().phase,
        admission_paused=gate.closed if gate is not None else None,
        active_matrix_operations=gate.in_flight_response_count if gate is not None else None,
        active_openai_requests=len(state.openai_responses),
        active_calls=len(calls) if calls is not None else None,
        interruptible_script_runs=interruptible_script_runs,
        recoverable_script_runs=recoverable_script_runs,
        responses=responses,
        script_runs=script_runs or [],
    )


def _activity_json_response(snapshot: ResponseActivity) -> JSONResponse:
    """Serialize one activity snapshot with its shared status policy."""
    return JSONResponse(
        snapshot.model_dump(),
        status_code=503 if snapshot.status == "unavailable" else 200,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/activity")
async def response_activity(request: Request) -> JSONResponse:
    """Read live counters without scanning logs, history, or Matrix state."""
    snapshot = await _response_activity_snapshot(request)
    aggregate = ResponseActivity.model_validate(
        snapshot.model_dump(exclude={"status", "responses", "script_runs"}),
    )
    return _activity_json_response(aggregate)


@router.get("/activity/details")
async def detailed_response_activity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Return operator-authenticated active response, call, and script identities."""
    require_operator_key(request, authorization)
    return _activity_json_response(await _response_activity_snapshot(request))
