"""Chat clients' room-scoped read and owner-only toggle/delete API for triggers."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.external_triggers.store import (
    ExternalTriggerForbiddenError,
    ExternalTriggerNotFoundError,
    ExternalTriggerRecord,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.plugin_http import RoomReader, client_rooms, require_room_reader
from mindroom.reactive_triggers.management import (
    delete_trigger_sync,
    is_trigger_owner,
    read_thread_tag_present,
    set_trigger_enabled_sync,
)
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    RoomMessageSourceSpec,
    ThreadTagSourceSpec,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.room_steward import room_steward
from mindroom.thread_tags import ThreadTagsError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["room-triggers"])
logger = get_logger(__name__)

_Room = Annotated[RoomReader, Depends(require_room_reader)]

_TriggerKind = Literal["webhook", "room_message", "thread_tag", "external_event"]
_TriggerRelation = Literal["target", "watch"]
_TriggerThreadMode = Literal["room", "thread", "new_thread", "origin"]


@dataclass(frozen=True)
class TriggerRow:
    """Allowlisted projection of one webhook or reactive trigger for a Matrix room."""

    trigger_id: str
    kind: _TriggerKind
    relation: _TriggerRelation
    description: str
    prompt: str | None
    agent: str | None
    target_room_id: str | None
    target_thread_id: str | None
    thread_mode: _TriggerThreadMode
    enabled: bool
    disabled_reason: str | None
    once: bool
    expires_at: int | None
    last_fired_at: int | None
    owner_user_id: str
    can_manage: bool


@dataclass(frozen=True)
class RoomTriggersResponse:
    """Room-scoped list of triggers targeting or watching a room."""

    triggers: list[TriggerRow]


class UpdateRoomTriggerRequest(BaseModel):
    """Request body for toggling one trigger's enabled state."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


@dataclass(frozen=True)
class _MatchedTrigger:
    record: ExternalTriggerRecord | ReactiveTriggerRecord
    relation: _TriggerRelation
    resolved_target_room_id: str


@router.get("/triggers")
async def list_room_triggers(request: Request, room: _Room, room_id: str) -> RoomTriggersResponse:
    """Return triggers targeting or watching ``room_id`` with cross-room watch redaction."""
    config, runtime_paths = _require_trigger_runtime(request)
    matched = await _run_store(lambda: _list_room_triggers_sync(runtime_paths, room_id))
    ownership = [
        is_trigger_owner(room.user.user_id, item.record.owner_user_id, config, runtime_paths) for item in matched
    ]
    watch_target_rooms = list(
        dict.fromkeys(
            item.resolved_target_room_id
            for item, can_manage in zip(matched, ownership, strict=True)
            if item.relation == "watch" and not can_manage
        ),
    )
    joined_target_rooms = set(await client_rooms(request, watch_target_rooms)) if watch_target_rooms else set()
    rows = [
        _build_trigger_row(item, can_manage=can_manage, joined_target_rooms=joined_target_rooms)
        for item, can_manage in zip(matched, ownership, strict=True)
    ]
    return RoomTriggersResponse(triggers=rows)


@router.patch("/triggers/{trigger_id}")
async def update_room_trigger(
    request: Request,
    room: _Room,
    room_id: str,
    trigger_id: str,
    body: UpdateRoomTriggerRequest,
) -> TriggerRow:
    """Enable or disable one trigger belonging to ``room_id``; owner only."""
    config, runtime_paths = _require_trigger_runtime(request)
    matched = await _run_store(lambda: _require_room_trigger(runtime_paths, room_id, trigger_id))
    if not is_trigger_owner(room.user.user_id, matched.record.owner_user_id, config, runtime_paths):
        raise HTTPException(403, "Only the trigger owner can manage this trigger.")

    refreshed_tag_present: bool | None = None
    if (
        isinstance(matched.record, ReactiveTriggerRecord)
        and body.enabled
        and not matched.record.enabled
        and isinstance(matched.record.source, ThreadTagSourceSpec)
    ):
        refreshed_tag_present = await _read_watched_tag_present(request, matched.record.source, runtime_paths)

    updated = await _run_store(
        lambda: _set_room_trigger_enabled_sync(
            runtime_paths,
            config,
            room_id,
            matched,
            enabled=body.enabled,
            actor_user_id=room.user.user_id,
            refreshed_tag_present=refreshed_tag_present,
        ),
    )
    if body.enabled and isinstance(updated.record, ReactiveTriggerRecord):
        _notify_reactive_event_loop(request, trigger_id)
    return _build_trigger_row(updated, can_manage=True, joined_target_rooms=set())


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_room_trigger(request: Request, room: _Room, room_id: str, trigger_id: str) -> Response:
    """Delete one trigger belonging to ``room_id``; owner only."""
    config, runtime_paths = _require_trigger_runtime(request)
    await _run_store(lambda: _delete_room_trigger_sync(runtime_paths, config, room_id, trigger_id, room.user.user_id))
    return Response(status_code=204)


def _require_trigger_runtime(request: Request) -> tuple[Config, RuntimePaths]:
    try:
        snapshot = bind_current_request_snapshot(request)
    except TypeError as exc:
        raise HTTPException(503, "Trigger configuration is not available.") from exc
    if snapshot.runtime_config is None:
        raise HTTPException(503, "Trigger configuration is not available.")
    if snapshot.runtime_paths.control_state_root is None:
        raise HTTPException(503, "Trigger store is not available.")
    return snapshot.runtime_config, snapshot.runtime_paths


async def _run_store[T](call: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(call)
    except ExternalTriggerNotFoundError as exc:
        raise HTTPException(404, "Trigger not found in this room.") from exc
    except ExternalTriggerForbiddenError as exc:
        raise HTTPException(403, "Only the trigger owner can manage this trigger.") from exc
    except (ExternalTriggerStoreError, ReactiveTriggerStoreError, sqlite3.Error) as exc:
        raise HTTPException(503, "Trigger store is not available.") from exc


def _match_trigger_to_room(
    record: ExternalTriggerRecord | ReactiveTriggerRecord,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> _MatchedTrigger | None:
    resolved_target_room_id = resolve_room_id(record.target.room_id, runtime_paths)
    if resolved_target_room_id == room_id:
        return _MatchedTrigger(record=record, relation="target", resolved_target_room_id=resolved_target_room_id)
    if (
        isinstance(record, ReactiveTriggerRecord)
        and isinstance(record.source, (RoomMessageSourceSpec, ThreadTagSourceSpec))
        and resolve_room_id(record.source.room_id, runtime_paths) == room_id
    ):
        return _MatchedTrigger(record=record, relation="watch", resolved_target_room_id=resolved_target_room_id)
    return None


def _list_room_triggers_sync(runtime_paths: RuntimePaths, room_id: str) -> list[_MatchedTrigger]:
    records: list[ExternalTriggerRecord | ReactiveTriggerRecord] = [
        *ExternalTriggerStore(runtime_paths).list_records(),
        *ReactiveTriggerStore(runtime_paths).list_triggers(include_disabled=True),
    ]
    matched = [item for record in records if (item := _match_trigger_to_room(record, room_id, runtime_paths))]
    matched.sort(
        key=lambda item: (0 if item.relation == "target" else 1, float(item.record.created_at), item.record.trigger_id),
    )
    return matched


def _require_room_trigger(
    runtime_paths: RuntimePaths,
    room_id: str,
    trigger_id: str,
    *,
    external_store: ExternalTriggerStore | None = None,
    reactive_store: ReactiveTriggerStore | None = None,
) -> _MatchedTrigger:
    ext = external_store or ExternalTriggerStore(runtime_paths)
    react = reactive_store or ReactiveTriggerStore(runtime_paths)
    record: ExternalTriggerRecord | ReactiveTriggerRecord | None = ext.get_record(trigger_id)
    if record is None:
        record = react.get_trigger(trigger_id)
    matched = None if record is None else _match_trigger_to_room(record, room_id, runtime_paths)
    if matched is None:
        msg = f"trigger not found in room: {trigger_id}"
        raise ExternalTriggerNotFoundError(msg)
    return matched


def _set_room_trigger_enabled_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    room_id: str,
    matched: _MatchedTrigger,
    *,
    enabled: bool,
    actor_user_id: str,
    refreshed_tag_present: bool | None,
) -> _MatchedTrigger:
    external_store = ExternalTriggerStore(runtime_paths)
    reactive_store = ReactiveTriggerStore(runtime_paths)
    trigger_id = matched.record.trigger_id
    is_reactive = isinstance(matched.record, ReactiveTriggerRecord)
    if is_reactive:
        live = _require_room_trigger(
            runtime_paths,
            room_id,
            trigger_id,
            external_store=external_store,
            reactive_store=reactive_store,
        )
        if not isinstance(live.record, ReactiveTriggerRecord) or live.record.rule_uid != matched.record.rule_uid:
            msg = f"trigger not found in room: {trigger_id}"
            raise ExternalTriggerNotFoundError(msg)
    updated = set_trigger_enabled_sync(
        external_store,
        reactive_store,
        trigger_id,
        enabled=enabled,
        actor_user_id=actor_user_id,
        config=config,
        runtime_paths=runtime_paths,
        allow_admin=False,
        refreshed_tag_present=refreshed_tag_present,
        expected_external_uid=None if is_reactive else matched.record.uid,
        expected_reactive_uid=matched.record.rule_uid if is_reactive else None,
    )
    updated_matched = _match_trigger_to_room(updated, room_id, runtime_paths)
    if updated_matched is None:
        msg = f"trigger not found in room: {trigger_id}"
        raise ExternalTriggerNotFoundError(msg)
    return updated_matched


def _delete_room_trigger_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    room_id: str,
    trigger_id: str,
    actor_user_id: str,
) -> None:
    external_store = ExternalTriggerStore(runtime_paths)
    reactive_store = ReactiveTriggerStore(runtime_paths)
    matched = _require_room_trigger(
        runtime_paths,
        room_id,
        trigger_id,
        external_store=external_store,
        reactive_store=reactive_store,
    )
    delete_trigger_sync(
        external_store,
        reactive_store,
        trigger_id,
        actor_user_id=actor_user_id,
        config=config,
        runtime_paths=runtime_paths,
        allow_admin=False,
        expected_external_uid=matched.record.uid if isinstance(matched.record, ExternalTriggerRecord) else None,
        expected_reactive_uid=matched.record.rule_uid if isinstance(matched.record, ReactiveTriggerRecord) else None,
    )


async def _read_watched_tag_present(
    request: Request,
    source: ThreadTagSourceSpec,
    runtime_paths: RuntimePaths,
) -> bool:
    bots = ensure_app_state(request.app).room_bots
    steward = None if bots is None else room_steward(bots, resolve_room_id(source.room_id, runtime_paths))
    if steward is None or steward.client is None:
        raise HTTPException(503, "No running MindRoom bot is in the watched room.")
    try:
        return await read_thread_tag_present(steward.client, source)
    except ThreadTagsError as exc:
        raise HTTPException(503, "Watched thread tags could not be read; try again.") from exc


def _notify_reactive_event_loop(request: Request, trigger_id: str) -> None:
    runtime = ensure_app_state(request.app).external_trigger_runtime
    if runtime is None or runtime.on_reactive_event is None:
        return
    try:
        runtime.on_reactive_event()
    except Exception:
        logger.warning(
            "Failed to notify reactive trigger loop after room trigger update",
            trigger_id=trigger_id,
            exc_info=True,
        )


def _thread_placement(target: ExternalTriggerTarget | TriggerTargetSpec) -> tuple[_TriggerThreadMode, str | None]:
    if target.new_thread:
        return "new_thread", None
    if target.thread_id == "origin":
        return "origin", None
    if target.thread_id is not None:
        return "thread", target.thread_id
    return "room", None


def _to_epoch_ms(timestamp_seconds: float | None) -> int | None:
    return None if timestamp_seconds is None else int(timestamp_seconds * 1000)


def _build_trigger_row(
    matched: _MatchedTrigger,
    *,
    can_manage: bool,
    joined_target_rooms: set[str],
) -> TriggerRow:
    record = matched.record
    redacted = (
        matched.relation == "watch" and not can_manage and matched.resolved_target_room_id not in joined_target_rooms
    )
    thread_mode, target_thread_id = _thread_placement(record.target)
    if isinstance(record, ExternalTriggerRecord):
        kind: _TriggerKind = "webhook"
        prompt: str | None = None
        disabled_reason: str | None = None if record.enabled else "manual"
        once = record.delivery_mode == "single_use"
        expires_at: int | None = None
    else:
        kind = record.source.source_type
        prompt = None if redacted else record.target.prompt
        disabled_reason = record.disabled_reason
        once = record.once
        expires_at = _to_epoch_ms(record.expires_at)

    return TriggerRow(
        trigger_id=record.trigger_id,
        kind=kind,
        relation=matched.relation,
        description="" if redacted else record.description,
        prompt=prompt,
        agent=None if redacted else record.target.agent,
        target_room_id=None if redacted else matched.resolved_target_room_id,
        target_thread_id=None if redacted else target_thread_id,
        thread_mode=thread_mode,
        enabled=record.enabled,
        disabled_reason=disabled_reason,
        once=once,
        expires_at=expires_at,
        last_fired_at=_to_epoch_ms(record.last_fired_at),
        owner_user_id=record.owner_user_id,
        can_manage=can_manage,
    )
