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


def _response_activity_snapshot(request: Request) -> ResponseActivity:
    """Capture shared aggregate fields from live process state."""
    state = config_lifecycle.app_state(request.app)
    gate = state.response_admission_gate
    return ResponseActivity(
        runtime_phase=get_runtime_state().phase,
        admission_paused=gate.closed if gate is not None else None,
        active_matrix_operations=gate.in_flight_response_count if gate is not None else None,
        active_openai_requests=len(state.openai_responses),
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
    """Read live counters on the runtime loop without scanning logs or storage."""
    return _activity_json_response(_response_activity_snapshot(request))


@router.get("/activity/details")
async def detailed_response_activity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Return operator-authenticated active response identities."""
    require_operator_key(request, authorization)

    aggregate = _response_activity_snapshot(request)
    state = config_lifecycle.app_state(request.app)
    gate = state.response_admission_gate
    responses = [
        ActiveResponseInfo(channel=channel, responder=identity.responder, requester_id=identity.requester_id)
        for channel, identities in (
            ("matrix", gate.response_identities if gate is not None else ()),
            ("openai", state.openai_responses),
        )
        for identity in identities
    ]

    snapshot = DetailedResponseActivity.model_validate(
        {
            **aggregate.model_dump(exclude={"status"}),
            "responses": responses,
        },
    )
    return _activity_json_response(snapshot)
