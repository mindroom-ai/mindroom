"""Public bearer-token API endpoint for one-shot callback delivery."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from mindroom.api import config_lifecycle
from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.callbacks.executor import execute_callback_fire
from mindroom.callbacks.models import CallbackAcceptedResponse, CallbackFirePayload
from mindroom.callbacks.store import (
    CallbackConsumedError,
    CallbackExpiredError,
    CallbackNotFoundError,
    CallbackRecordNotDeliverableError,
    CallbackStore,
    CallbackStoreError,
    token_matches_hash,
)
from mindroom.external_triggers.executor import is_user_joined_room
from mindroom.external_triggers.models import TriggerDeliveryReadiness
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

    from mindroom.callbacks.store import CallbackDeliverySnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

router = APIRouter(prefix="/api/callbacks", tags=["callbacks"])
logger = get_logger(__name__)


def _not_found_error() -> HTTPException:
    """Return the shared not-found error that never leaks callback existence."""
    return HTTPException(status_code=404, detail="Callback not found")


@router.post("/{callback_id}", response_model=CallbackAcceptedResponse)
async def post_callback(callback_id: str, request: Request) -> CallbackAcceptedResponse:
    """Accept one bearer-authenticated callback fire and dispatch it into Matrix."""
    config, runtime_paths, snapshot = await _request_config_and_callback_snapshot(callback_id, request)
    _require_matching_token(request, snapshot)
    now = int(time.time())
    if snapshot.uses_left <= 0:
        raise HTTPException(status_code=410, detail="Callback has already been used")
    if snapshot.expires_at <= now:
        raise HTTPException(status_code=410, detail="Callback has expired")

    body = await _read_bounded_body(request, max_body_bytes=config.callback_policy.max_body_bytes)
    payload = _parse_payload(body)
    _validate_snapshot_policy_and_auth(snapshot, config, runtime_paths)

    runtime = await _require_callback_runtime(request, snapshot)
    owner_joined = await is_user_joined_room(
        cast("nio.AsyncClient", runtime.client),
        snapshot.resolved_room_id,
        snapshot.owner_user_id,
    )
    if not owner_joined:
        raise HTTPException(status_code=403, detail="Callback owner is not joined to the target room")

    return await _claim_and_execute_callback(
        payload=payload,
        snapshot=snapshot,
        config=config,
        runtime_paths=runtime_paths,
        runtime=runtime,
    )


async def _request_config_and_callback_snapshot(
    callback_id: str,
    request: Request,
) -> tuple[Config, RuntimePaths, CallbackDeliverySnapshot]:
    api_snapshot = _bind_request_api_snapshot(request)
    try:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail="Callback configuration is not available") from exc
    if not config.callback_policy.enabled:
        raise _not_found_error()

    callback_store = _callback_store(runtime_paths)
    try:
        snapshot = await asyncio.to_thread(
            callback_store.delivery_snapshot,
            callback_id,
            config=config,
            config_generation=api_snapshot.generation,
        )
    except CallbackRecordNotDeliverableError as exc:
        raise _not_found_error() from exc
    except CallbackStoreError as exc:
        raise HTTPException(status_code=503, detail="Callback store is not available") from exc
    if snapshot is None:
        raise _not_found_error()
    return config, runtime_paths, snapshot


def _bind_request_api_snapshot(request: Request) -> config_lifecycle.ApiSnapshot:
    try:
        return config_lifecycle.bind_current_request_snapshot(request)
    except TypeError as exc:
        raise HTTPException(status_code=503, detail="Callback configuration is not available") from exc


def _require_matching_token(request: Request, snapshot: CallbackDeliverySnapshot) -> None:
    """Reject missing or wrong bearer tokens without leaking callback existence."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _not_found_error()
    if not token_matches_hash(token.strip(), snapshot.token_hash):
        raise _not_found_error()


async def _claim_and_execute_callback(
    *,
    payload: CallbackFirePayload,
    snapshot: CallbackDeliverySnapshot,
    config: Config,
    runtime_paths: RuntimePaths,
    runtime: config_lifecycle.ExternalTriggerRuntime,
) -> CallbackAcceptedResponse:
    callback_store = _callback_store(runtime_paths)
    try:
        uses_left = await asyncio.to_thread(callback_store.claim_use, snapshot.callback_id, now=int(time.time()))
    except CallbackNotFoundError as exc:
        raise _not_found_error() from exc
    except (CallbackConsumedError, CallbackExpiredError) as exc:
        raise HTTPException(status_code=410, detail=str(exc).capitalize()) from exc
    except CallbackStoreError as exc:
        raise HTTPException(status_code=503, detail="Callback store is not available") from exc

    try:
        matrix_event_id = await execute_callback_fire(
            client=cast("nio.AsyncClient", runtime.client),
            snapshot=snapshot,
            payload=payload,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=cast("ConversationCacheProtocol", runtime.conversation_cache),
        )
    except Exception:
        await _release_use_best_effort(callback_store, snapshot.callback_id)
        raise
    if matrix_event_id is None:
        await _release_use_best_effort(callback_store, snapshot.callback_id)
        raise HTTPException(status_code=502, detail="Callback delivery failed")

    return CallbackAcceptedResponse(
        accepted=True,
        callback_id=snapshot.callback_id,
        uses_left=uses_left,
        matrix_event_id=matrix_event_id,
    )


def _callback_store(runtime_paths: RuntimePaths) -> CallbackStore:
    if runtime_paths.control_state_root is None:
        raise HTTPException(status_code=503, detail="Callback store is not available")
    return CallbackStore(runtime_paths)


def _validate_snapshot_policy_and_auth(
    snapshot: CallbackDeliverySnapshot,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    if not is_authorized_sender(snapshot.owner_user_id, config, snapshot.resolved_room_id, runtime_paths):
        raise HTTPException(status_code=403, detail="Callback owner is not authorized for this room")
    if not is_sender_allowed_for_agent_reply(
        snapshot.owner_user_id,
        snapshot.target_agent,
        config,
        runtime_paths,
    ):
        raise HTTPException(status_code=403, detail="Callback owner is not authorized for this target")


async def _read_bounded_body(request: Request, *, max_body_bytes: int) -> bytes:
    body_chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise HTTPException(status_code=413, detail="Callback body exceeds configured limit")
        body_chunks.append(chunk)
    return b"".join(body_chunks)


def _parse_payload(body: bytes) -> CallbackFirePayload:
    try:
        return CallbackFirePayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc


async def _require_callback_runtime(
    request: Request,
    snapshot: CallbackDeliverySnapshot,
) -> config_lifecycle.ExternalTriggerRuntime:
    runtime = config_lifecycle.app_state(request.app).external_trigger_runtime
    if runtime is None or runtime.config_generation != snapshot.config_generation:
        raise HTTPException(status_code=503, detail="Callback runtime is not available")
    readiness = TriggerDeliveryReadiness(
        enabled=True,
        target_agent=snapshot.target_agent,
        resolved_room_id=snapshot.resolved_room_id,
    )
    try:
        is_target_ready = await runtime.is_delivery_target_ready(readiness)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Callback target runtime is not available") from exc
    if not is_target_ready:
        raise HTTPException(status_code=503, detail="Callback target runtime is not available")
    return runtime


async def _release_use_best_effort(store: CallbackStore, callback_id: str) -> None:
    """Return a claimed use without masking the delivery failure."""
    try:
        await asyncio.to_thread(store.release_use, callback_id)
    except Exception:
        logger.warning(
            "Failed to release callback use after delivery failure",
            callback_id=callback_id,
            exc_info=True,
        )
