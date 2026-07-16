"""Public endpoint for single-use completion callbacks."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from mindroom.api import config_lifecycle
from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.callbacks.executor import execute_callback_fire
from mindroom.callbacks.store import (
    CallbackClaimedError,
    CallbackExpiredError,
    CallbackNotFoundError,
    CallbackRecord,
    CallbackStore,
    CallbackStoreError,
    token_matches_hash,
)
from mindroom.external_triggers.executor import is_user_joined_room
from mindroom.external_triggers.models import TriggerDeliveryReadiness
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

router = APIRouter(prefix="/api/callbacks", tags=["callbacks"])
logger = get_logger(__name__)
_MAX_BODY_BYTES = 65_536
_CompletionMessage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]


class _CallbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: _CompletionMessage


def _not_found_error() -> HTTPException:
    return HTTPException(status_code=404, detail="Callback not found")


@router.post("/{callback_id}", status_code=204)
async def post_callback(callback_id: str, request: Request) -> Response:
    """Wake the conversation bound to one valid callback."""
    config, runtime_paths, config_generation, record = await _request_callback(callback_id, request)
    _require_matching_token(request, record)
    body = await _read_bounded_body(request)
    payload = _parse_payload(body)
    _validate_owner_authorization(record, config, runtime_paths)

    runtime = await _require_callback_runtime(request, record, config_generation=config_generation)
    owner_joined = await is_user_joined_room(
        cast("nio.AsyncClient", runtime.client),
        record.room_id,
        record.owner_user_id,
    )
    if not owner_joined:
        raise HTTPException(status_code=403, detail="Callback owner is not joined to the target room")

    await _claim_and_deliver(
        record=record,
        payload=payload,
        config=config,
        runtime_paths=runtime_paths,
        runtime=runtime,
    )
    return Response(status_code=204)


async def _request_callback(
    callback_id: str,
    request: Request,
) -> tuple[Config, RuntimePaths, int, CallbackRecord]:
    try:
        api_snapshot = config_lifecycle.bind_current_request_snapshot(request)
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except (HTTPException, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Callback configuration is not available") from exc
    store = _callback_store(runtime_paths)
    try:
        record = await asyncio.to_thread(store.get_record, callback_id)
    except CallbackStoreError as exc:
        raise HTTPException(status_code=503, detail="Callback store is not available") from exc
    if record is None:
        raise _not_found_error()
    return config, runtime_paths, api_snapshot.generation, record


def _require_matching_token(request: Request, record: CallbackRecord) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _not_found_error()
    if not token_matches_hash(token.strip(), record.token_hash):
        raise _not_found_error()


async def _claim_and_deliver(
    *,
    record: CallbackRecord,
    payload: _CallbackPayload,
    config: Config,
    runtime_paths: RuntimePaths,
    runtime: config_lifecycle.ExternalTriggerRuntime,
) -> None:
    store = _callback_store(runtime_paths)
    try:
        claimed = await asyncio.to_thread(store.claim, record.callback_id, now=int(time.time()))
    except CallbackNotFoundError as exc:
        raise _not_found_error() from exc
    except (CallbackClaimedError, CallbackExpiredError) as exc:
        raise HTTPException(status_code=410, detail=str(exc).capitalize()) from exc
    except CallbackStoreError as exc:
        raise HTTPException(status_code=503, detail="Callback store is not available") from exc

    try:
        matrix_event_id = await execute_callback_fire(
            client=cast("nio.AsyncClient", runtime.client),
            record=claimed,
            message=payload.message,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=cast("ConversationCacheProtocol", runtime.conversation_cache),
        )
    except Exception:
        await _release_claim_best_effort(store, record.callback_id)
        raise
    if matrix_event_id is None:
        await _release_claim_best_effort(store, record.callback_id)
        raise HTTPException(status_code=502, detail="Callback delivery failed")
    await _delete_record_best_effort(store, record.callback_id)


def _callback_store(runtime_paths: RuntimePaths) -> CallbackStore:
    if runtime_paths.control_state_root is None:
        raise HTTPException(status_code=503, detail="Callback store is not available")
    return CallbackStore(runtime_paths)


def _validate_owner_authorization(record: CallbackRecord, config: Config, runtime_paths: RuntimePaths) -> None:
    if not is_authorized_sender(record.owner_user_id, config, record.room_id, runtime_paths):
        raise HTTPException(status_code=403, detail="Callback owner is not authorized for this room")
    if not is_sender_allowed_for_agent_reply(record.owner_user_id, record.agent_name, config, runtime_paths):
        raise HTTPException(status_code=403, detail="Callback owner is not authorized for this target")


async def _read_bounded_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Callback body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_payload(body: bytes) -> _CallbackPayload:
    try:
        return _CallbackPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc


async def _require_callback_runtime(
    request: Request,
    record: CallbackRecord,
    *,
    config_generation: int,
) -> config_lifecycle.ExternalTriggerRuntime:
    runtime = config_lifecycle.app_state(request.app).external_trigger_runtime
    if runtime is None or runtime.config_generation != config_generation:
        raise HTTPException(status_code=503, detail="Callback runtime is not available")
    readiness = TriggerDeliveryReadiness(
        enabled=True,
        target_agent=record.agent_name,
        resolved_room_id=record.room_id,
    )
    try:
        ready = await runtime.is_delivery_target_ready(readiness)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Callback target runtime is not available") from exc
    if not ready:
        raise HTTPException(status_code=503, detail="Callback target runtime is not available")
    return runtime


async def _release_claim_best_effort(store: CallbackStore, callback_id: str) -> None:
    try:
        await asyncio.to_thread(store.release, callback_id)
    except Exception:
        logger.warning("Failed to release callback claim", callback_id=callback_id, exc_info=True)


async def _delete_record_best_effort(store: CallbackStore, callback_id: str) -> None:
    try:
        await asyncio.to_thread(store.delete, callback_id)
    except Exception:
        logger.warning("Failed to delete delivered callback", callback_id=callback_id, exc_info=True)
