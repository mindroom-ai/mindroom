"""Read-only response activity for the bundled runtime."""

from __future__ import annotations

import secrets
from collections import Counter
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.response_activity import ActiveResponseInfo, DetailedResponseActivity, ResponseActivity
from mindroom.response_tracking import ResponseIdentity, ResponseTrackingHandle
from mindroom.runtime_state import get_runtime_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/api/responses", tags=["responses"])
_OPENAI_TRACKING_HANDLE_SCOPE_KEY = "mindroom.response_activity.openai_tracking_handle"


async def track_openai_request(request: Request) -> AsyncIterator[None]:
    """Keep an OpenAI request counted until its response body has finished."""
    state = config_lifecycle.app_state(request.app)
    with state.openai_response_tracker.track() as handle:
        request.scope[_OPENAI_TRACKING_HANDLE_SCOPE_KEY] = handle
        try:
            yield
        finally:
            request.scope.pop(_OPENAI_TRACKING_HANDLE_SCOPE_KEY, None)


def update_openai_response_identity(
    request: Request,
    *,
    responder: str | None = None,
    requester_id: str | None = None,
) -> None:
    """Refine canonical metadata for the request-scoped OpenAI activity slot."""
    handle = request.scope.get(_OPENAI_TRACKING_HANDLE_SCOPE_KEY)
    if not isinstance(handle, ResponseTrackingHandle):
        return
    current = handle.identity
    handle.identity = ResponseIdentity(
        responder=responder if responder is not None else current.responder,
        requester_id=requester_id if requester_id is not None else current.requester_id,
    )


@router.get("/activity")
async def response_activity(request: Request) -> JSONResponse:
    """Read live counters on the runtime loop without scanning logs or storage."""
    state = config_lifecycle.app_state(request.app)
    gate = state.response_admission_gate
    snapshot = ResponseActivity(
        runtime_phase=get_runtime_state().phase,
        admission_paused=gate.closed if gate is not None else None,
        active_matrix_operations=gate.active_operation_count if gate is not None else None,
        active_openai_requests=state.openai_response_tracker.count,
    )
    return JSONResponse(
        snapshot.model_dump(),
        status_code=503 if snapshot.status == "unavailable" else 200,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/activity/details")
async def detailed_response_activity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Return operator-authenticated active response identities."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    configured_key = runtime_paths.env_value("MINDROOM_API_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="Response activity details require MINDROOM_API_KEY")
    token = (
        authorization.removeprefix("Bearer ").strip() if authorization and authorization.startswith("Bearer ") else None
    )
    if token is None or not secrets.compare_digest(token, configured_key):
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")

    state = config_lifecycle.app_state(request.app)
    gate = state.response_admission_gate
    matrix_count = gate.active_operation_count if gate is not None else None
    matrix_identities = gate.response_tracker.snapshot() if gate is not None else ()
    openai_count = state.openai_response_tracker.count
    openai_identities = state.openai_response_tracker.snapshot()

    responses: list[ActiveResponseInfo] = []
    for channel, total, identities in (
        ("matrix", matrix_count or 0, matrix_identities),
        ("openai", openai_count, openai_identities),
    ):
        responses.extend(
            ActiveResponseInfo(
                channel=channel,
                responder=identity.responder,
                requester_id=identity.requester_id,
                operations=operations,
            )
            for identity, operations in Counter(identities).items()
        )
        unknown_operations = total - len(identities)
        if unknown_operations > 0:
            responses.append(
                ActiveResponseInfo(
                    channel=channel,
                    responder=None,
                    requester_id=None,
                    operations=unknown_operations,
                ),
            )

    snapshot = DetailedResponseActivity(
        runtime_phase=get_runtime_state().phase,
        admission_paused=gate.closed if gate is not None else None,
        active_matrix_operations=matrix_count,
        active_openai_requests=openai_count,
        responses=responses,
    )
    return JSONResponse(
        snapshot.model_dump(),
        status_code=503 if snapshot.status == "unavailable" else 200,
        headers={"Cache-Control": "no-store"},
    )
