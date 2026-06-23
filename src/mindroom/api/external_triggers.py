"""Public signed API endpoint for external trigger delivery."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.external_triggers.auth import TriggerAuthError, TriggerSignatureHeaders, verify_trigger_request
from mindroom.external_triggers.executor import execute_external_trigger
from mindroom.external_triggers.models import ExternalTriggerAcceptedResponse, ExternalTriggerPayload
from mindroom.external_triggers.replay_store import ExternalTriggerEventClaim, ExternalTriggerReplayStore

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

router = APIRouter(prefix="/api/triggers", tags=["external-triggers"])
_IN_PROGRESS_EVENT_ID_TTL_SECONDS = 86400
_DELIVERED_EVENT_ID_TTL_SECONDS = 86400


@router.post(
    "/{trigger_id}",
    status_code=202,
    response_model=ExternalTriggerAcceptedResponse,
)
async def post_external_trigger(trigger_id: str, request: Request) -> ExternalTriggerAcceptedResponse:
    """Accept one signed external trigger and dispatch it into Matrix."""
    try:
        snapshot = config_lifecycle.bind_current_request_snapshot(request)
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except (HTTPException, TypeError) as exc:
        raise HTTPException(status_code=503, detail="External trigger configuration is not available") from exc
    trigger = config.external_triggers.get(trigger_id)
    if trigger is None or not trigger.enabled:
        raise HTTPException(status_code=404, detail="External trigger not found")

    body = await _read_bounded_body(request, max_body_bytes=trigger.max_body_bytes)
    signature_headers = _verified_signature_headers(
        request,
        body=body,
        expected_key_id=trigger.key_id,
        public_key_b64=trigger.public_key,
        replay_window_seconds=trigger.replay_window_seconds,
    )
    payload = _parse_payload(body)
    if trigger.allowed_kinds and payload.kind not in trigger.allowed_kinds:
        raise HTTPException(status_code=422, detail="External trigger kind is not allowed")

    now = int(time.time())
    store = ExternalTriggerReplayStore(constants.tracking_dir(runtime_paths))
    event_id = payload.event_id or signature_headers.nonce
    if not store.claim_nonce(trigger_id, signature_headers.nonce, now=now, ttl_seconds=trigger.replay_window_seconds):
        raise HTTPException(status_code=409, detail="External trigger nonce has already been used")

    if store.event_id_is_delivered(trigger_id, event_id, now=now):
        return ExternalTriggerAcceptedResponse(
            accepted=True,
            duplicate=True,
            trigger_id=trigger_id,
            event_id=event_id,
        )

    runtime = _require_external_trigger_runtime(request, snapshot.generation, trigger_id)

    event_claim = store.claim_event_id(
        trigger_id,
        event_id,
        now=now,
        ttl_seconds=_IN_PROGRESS_EVENT_ID_TTL_SECONDS,
    )
    if event_claim is ExternalTriggerEventClaim.DELIVERED:
        return ExternalTriggerAcceptedResponse(
            accepted=True,
            duplicate=True,
            trigger_id=trigger_id,
            event_id=event_id,
        )
    if event_claim is ExternalTriggerEventClaim.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="External trigger event is already in progress")

    payload = payload.model_copy(update={"event_id": event_id})
    try:
        matrix_event_id = await execute_external_trigger(
            client=cast("nio.AsyncClient", runtime.client),
            trigger_id=trigger_id,
            trigger=trigger,
            payload=payload,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=cast("ConversationCacheProtocol", runtime.conversation_cache),
        )
    except Exception:
        store.release_event_id(trigger_id, event_id)
        raise
    if matrix_event_id is None:
        store.release_event_id(trigger_id, event_id)
        raise HTTPException(status_code=502, detail="External trigger delivery failed")

    store.mark_event_delivered(
        trigger_id,
        event_id,
        now=int(time.time()),
        ttl_seconds=_DELIVERED_EVENT_ID_TTL_SECONDS,
    )
    return ExternalTriggerAcceptedResponse(
        accepted=True,
        duplicate=False,
        trigger_id=trigger_id,
        event_id=event_id,
        matrix_event_id=matrix_event_id,
    )


async def _read_bounded_body(request: Request, *, max_body_bytes: int) -> bytes:
    body_chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise HTTPException(status_code=413, detail="External trigger body exceeds configured limit")
        body_chunks.append(chunk)
    return b"".join(body_chunks)


def _verified_signature_headers(
    request: Request,
    *,
    body: bytes,
    expected_key_id: str,
    public_key_b64: str,
    replay_window_seconds: int,
) -> TriggerSignatureHeaders:
    try:
        signature_headers = TriggerSignatureHeaders.from_mapping(request.headers)
        verify_trigger_request(
            method=request.method,
            path=request.url.path,
            body=body,
            headers=signature_headers,
            expected_key_id=expected_key_id,
            public_key_b64=public_key_b64,
            replay_window_seconds=replay_window_seconds,
            now=int(time.time()),
        )
    except TriggerAuthError as exc:
        raise HTTPException(status_code=401, detail="Invalid external trigger signature") from exc
    return signature_headers


def _parse_payload(body: bytes) -> ExternalTriggerPayload:
    try:
        return ExternalTriggerPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc


def _require_external_trigger_runtime(
    request: Request,
    snapshot_generation: int,
    trigger_id: str,
) -> config_lifecycle.ExternalTriggerRuntime:
    runtime = config_lifecycle.app_state(request.app).external_trigger_runtime
    if runtime is None or runtime.config_generation != snapshot_generation:
        raise HTTPException(status_code=503, detail="External trigger runtime is not available")
    if trigger_id not in runtime.ready_trigger_ids:
        raise HTTPException(status_code=503, detail="External trigger target runtime is not available")
    return runtime
