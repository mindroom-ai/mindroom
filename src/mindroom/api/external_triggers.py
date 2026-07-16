"""Public API endpoint for authenticated external trigger delivery."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from mindroom.api import config_lifecycle
from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.external_triggers.auth import (
    TriggerAuthError,
    TriggerSignatureHeaders,
    trigger_capability_matches,
    verify_trigger_request,
)
from mindroom.external_triggers.executor import execute_external_trigger, is_external_trigger_owner_joined_target_room
from mindroom.external_triggers.models import (
    ExternalTriggerAcceptedResponse,
    ExternalTriggerPayload,
)
from mindroom.external_triggers.replay_store import (
    ExternalTriggerEventClaim,
    ExternalTriggerReplayStore,
    ExternalTriggerReplayStoreError,
)
from mindroom.external_triggers.store import (
    ExternalTriggerRecordNotDeliverableError,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    TriggerDeliverySnapshot,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

router = APIRouter(prefix="/api/triggers", tags=["external-triggers"])
logger = get_logger(__name__)
_IN_PROGRESS_EVENT_ID_TTL_SECONDS = 86400
_DELIVERED_EVENT_ID_TTL_SECONDS = 86400
_P = ParamSpec("_P")
_T = TypeVar("_T")


@router.post(
    "/{trigger_id}",
    status_code=202,
    response_model=ExternalTriggerAcceptedResponse,
)
async def post_external_trigger(trigger_id: str, request: Request) -> ExternalTriggerAcceptedResponse:
    """Accept one authenticated external trigger and dispatch it into Matrix."""
    config, runtime_paths, trigger_snapshot = await _request_config_and_trigger_snapshot(trigger_id, request)
    body = await _read_bounded_body(request, max_body_bytes=trigger_snapshot.max_body_bytes)
    signature_headers = _authenticate_trigger_request(
        request,
        body=body,
        snapshot=trigger_snapshot,
    )
    payload = _parse_payload(body)
    if trigger_snapshot.allowed_kinds and payload.kind not in trigger_snapshot.allowed_kinds:
        raise HTTPException(status_code=422, detail="External trigger kind is not allowed")
    _validate_snapshot_policy_and_auth(trigger_snapshot, config, runtime_paths)

    runtime = await _require_external_trigger_runtime(request, trigger_snapshot)
    await _require_owner_joined_target_room(runtime, trigger_snapshot)

    return await _claim_and_execute_trigger(
        payload=payload,
        signature_headers=signature_headers,
        snapshot=trigger_snapshot,
        config=config,
        runtime_paths=runtime_paths,
        runtime=runtime,
    )


async def _request_config_and_trigger_snapshot(
    trigger_id: str,
    request: Request,
) -> tuple[Config, RuntimePaths, TriggerDeliverySnapshot]:
    api_snapshot = _bind_request_api_snapshot(request)
    try:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail="External trigger configuration is not available") from exc
    if not config.external_trigger_policy.enabled:
        raise HTTPException(status_code=404, detail="External trigger not found")

    trigger_store = _trigger_store(runtime_paths)
    try:
        trigger_snapshot = await asyncio.to_thread(
            trigger_store.delivery_snapshot,
            trigger_id,
            config=config,
            config_generation=api_snapshot.generation,
        )
    except ExternalTriggerRecordNotDeliverableError as exc:
        raise HTTPException(status_code=404, detail="External trigger not found") from exc
    except ExternalTriggerStoreError as exc:
        raise HTTPException(status_code=503, detail="External trigger store is not available") from exc
    if trigger_snapshot is None or not trigger_snapshot.enabled:
        raise HTTPException(status_code=404, detail="External trigger not found")
    return config, runtime_paths, trigger_snapshot


def _bind_request_api_snapshot(request: Request) -> config_lifecycle.ApiSnapshot:
    try:
        return config_lifecycle.bind_current_request_snapshot(request)
    except TypeError as exc:
        raise HTTPException(status_code=503, detail="External trigger configuration is not available") from exc


async def _claim_and_execute_trigger(
    *,
    payload: ExternalTriggerPayload,
    signature_headers: TriggerSignatureHeaders | None,
    snapshot: TriggerDeliverySnapshot,
    config: Config,
    runtime_paths: RuntimePaths,
    runtime: config_lifecycle.ExternalTriggerRuntime,
) -> ExternalTriggerAcceptedResponse:
    now = int(time.time())
    event_id = (
        snapshot.uid
        if snapshot.delivery_mode == "single_use"
        else _reusable_event_id(
            payload,
            signature_headers,
        )
    )
    replay_store = _replay_store(runtime_paths)
    if signature_headers is not None and not await _run_replay_store_call(
        replay_store.claim_nonce,
        snapshot.replay_scope,
        signature_headers.nonce,
        now=now,
        ttl_seconds=snapshot.replay_window_seconds,
    ):
        raise HTTPException(status_code=409, detail="External trigger nonce has already been used")

    event_claim = await _run_replay_store_call(
        replay_store.claim_event_id,
        snapshot.replay_scope,
        event_id,
        now=now,
        ttl_seconds=_IN_PROGRESS_EVENT_ID_TTL_SECONDS,
    )
    if event_claim is ExternalTriggerEventClaim.DELIVERED:
        await _consume_single_use_trigger(snapshot, runtime_paths)
        return ExternalTriggerAcceptedResponse(
            accepted=True,
            duplicate=True,
            trigger_id=snapshot.trigger_id,
            event_id=event_id,
        )
    if event_claim is ExternalTriggerEventClaim.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="External trigger event is already in progress")

    payload = payload.model_copy(update={"event_id": event_id})
    try:
        matrix_event_id = await execute_external_trigger(
            client=cast("nio.AsyncClient", runtime.client),
            snapshot=snapshot,
            payload=payload,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=cast("ConversationCacheProtocol", runtime.conversation_cache),
        )
    except Exception:
        await _release_event_id_best_effort(replay_store, snapshot.replay_scope, event_id)
        raise
    if matrix_event_id is None:
        await _release_event_id_best_effort(replay_store, snapshot.replay_scope, event_id)
        raise HTTPException(status_code=502, detail="External trigger delivery failed")

    await _run_replay_store_call(
        replay_store.mark_event_delivered,
        snapshot.replay_scope,
        event_id,
        now=int(time.time()),
        ttl_seconds=_DELIVERED_EVENT_ID_TTL_SECONDS,
    )
    await _consume_single_use_trigger(snapshot, runtime_paths)
    return ExternalTriggerAcceptedResponse(
        accepted=True,
        duplicate=False,
        trigger_id=snapshot.trigger_id,
        event_id=event_id,
        matrix_event_id=matrix_event_id,
    )


def _trigger_store(runtime_paths: RuntimePaths) -> ExternalTriggerStore:
    if runtime_paths.control_state_root is None:
        raise HTTPException(status_code=503, detail="External trigger store is not available")
    return ExternalTriggerStore(runtime_paths)


def _replay_store(runtime_paths: RuntimePaths) -> ExternalTriggerReplayStore:
    if runtime_paths.control_state_root is None:
        raise HTTPException(status_code=503, detail="External trigger replay store is not available")
    return ExternalTriggerReplayStore(runtime_paths.control_state_root)


def _validate_snapshot_policy_and_auth(
    snapshot: TriggerDeliverySnapshot,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    if not is_authorized_sender(snapshot.owner_user_id, config, snapshot.resolved_room_id, runtime_paths):
        raise HTTPException(status_code=403, detail="External trigger owner is not authorized for this room")
    if not is_sender_allowed_for_agent_reply(
        snapshot.owner_user_id,
        snapshot.target.agent,
        config,
        runtime_paths,
    ):
        raise HTTPException(status_code=403, detail="External trigger owner is not authorized for this target")


async def _read_bounded_body(request: Request, *, max_body_bytes: int) -> bytes:
    body_chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise HTTPException(status_code=413, detail="External trigger body exceeds configured limit")
        body_chunks.append(chunk)
    return b"".join(body_chunks)


def _authenticate_trigger_request(
    request: Request,
    *,
    body: bytes,
    snapshot: TriggerDeliverySnapshot,
) -> TriggerSignatureHeaders | None:
    if snapshot.auth == "capability":
        _require_matching_capability(request, snapshot)
        return None
    if snapshot.key_id is None or snapshot.public_key is None:
        raise HTTPException(status_code=503, detail="External trigger signing key is not available")
    try:
        signature_headers = TriggerSignatureHeaders.from_mapping(request.headers)
        verify_trigger_request(
            method=request.method,
            path=request.url.path,
            body=body,
            headers=signature_headers,
            expected_key_id=snapshot.key_id,
            public_key_b64=snapshot.public_key,
            replay_window_seconds=snapshot.replay_window_seconds,
            now=int(time.time()),
        )
    except TriggerAuthError as exc:
        raise HTTPException(status_code=401, detail="Invalid external trigger signature") from exc
    return signature_headers


def _require_matching_capability(request: Request, snapshot: TriggerDeliverySnapshot) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    token_hash = snapshot.capability_token_hash
    if (
        scheme.lower() != "bearer"
        or not token.strip()
        or token_hash is None
        or not trigger_capability_matches(token.strip(), token_hash)
    ):
        raise HTTPException(status_code=404, detail="External trigger not found")


def _reusable_event_id(
    payload: ExternalTriggerPayload,
    signature_headers: TriggerSignatureHeaders | None,
) -> str:
    if payload.event_id is not None:
        return payload.event_id
    if signature_headers is None:
        raise HTTPException(status_code=422, detail="Reusable triggers require an event id")
    return signature_headers.nonce


async def _consume_single_use_trigger(snapshot: TriggerDeliverySnapshot, runtime_paths: RuntimePaths) -> None:
    if snapshot.delivery_mode != "single_use":
        return
    try:
        await asyncio.to_thread(
            _trigger_store(runtime_paths).consume_single_use,
            snapshot.trigger_id,
            expected_uid=snapshot.uid,
        )
    except ExternalTriggerStoreError as exc:
        raise HTTPException(status_code=503, detail="Single-use trigger could not be consumed") from exc


def _parse_payload(body: bytes) -> ExternalTriggerPayload:
    try:
        return ExternalTriggerPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc


async def _run_replay_store_call(call: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    try:
        return await asyncio.to_thread(call, *args, **kwargs)
    except ExternalTriggerReplayStoreError as exc:
        raise HTTPException(status_code=503, detail="External trigger replay store is not available") from exc


async def _release_event_id_best_effort(
    store: ExternalTriggerReplayStore,
    replay_scope: str,
    event_id: str,
) -> None:
    """Release an in-progress event claim without masking the delivery failure."""
    try:
        await asyncio.to_thread(store.release_event_id, replay_scope, event_id)
    except Exception:
        logger.warning(
            "Failed to release external trigger event claim after delivery failure",
            replay_scope=replay_scope,
            event_id=event_id,
            exc_info=True,
        )


async def _require_external_trigger_runtime(
    request: Request,
    snapshot: TriggerDeliverySnapshot,
) -> config_lifecycle.ExternalTriggerRuntime:
    runtime = config_lifecycle.app_state(request.app).external_trigger_runtime
    if runtime is None or runtime.config_generation != snapshot.config_generation:
        raise HTTPException(status_code=503, detail="External trigger runtime is not available")
    try:
        is_trigger_ready = await runtime.is_trigger_snapshot_ready(snapshot)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="External trigger target runtime is not available") from exc
    if not is_trigger_ready:
        raise HTTPException(status_code=503, detail="External trigger target runtime is not available")
    return runtime


async def _require_owner_joined_target_room(
    runtime: config_lifecycle.ExternalTriggerRuntime,
    snapshot: TriggerDeliverySnapshot,
) -> None:
    owner_joined = await is_external_trigger_owner_joined_target_room(
        cast("nio.AsyncClient", runtime.client),
        snapshot,
    )
    if not owner_joined:
        raise HTTPException(status_code=403, detail="External trigger owner is not joined to the target room")
