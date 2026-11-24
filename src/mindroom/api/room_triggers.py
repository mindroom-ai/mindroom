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
from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerStore, ExternalTriggerStoreError
from mindroom.matrix.state import resolve_room_id
from mindroom.plugin_http import RoomReader, client_rooms, require_room_reader
from mindroom.reactive_triggers.management import (
    delete_trigger_sync,
    is_trigger_owner,
    read_thread_tag_present,
    set_trigger_enabled_sync,
)
from mindroom.reactive_triggers.models import ReactiveTriggerRecord, RoomMessageSourceSpec, ThreadTagSourceSpec
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.room_steward import room_steward
from mindroom.thread_tags import ThreadTagsError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["room-triggers"])

_Room = Annotated[RoomReader, Depends(require_room_reader)]
_TriggerKind = Literal["webhook", "room_message", "thread_tag", "external_event"]
_TriggerRelation = Literal["target", "watch"]
_TriggerThreadMode = Literal["room", "thread", "new_thread", "origin"]
_TriggerRecord = ExternalTriggerRecord | ReactiveTriggerRecord
_MatchedTrigger = tuple[_TriggerRecord, _TriggerRelation, str]


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


class UpdateRoomTriggerRequest(BaseModel):
    """Request body for toggling one trigger's enabled state."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


@router.get("/triggers")
async def list_room_triggers(request: Request, room: _Room, room_id: str) -> dict[str, list[TriggerRow]]:
    """Return triggers targeting or watching ``room_id`` with cross-room watch redaction."""
    config, runtime_paths = _require_trigger_runtime(request)

    def _load() -> list[_MatchedTrigger]:
        records: list[_TriggerRecord] = [
            *ExternalTriggerStore(runtime_paths).list_records(),
            *ReactiveTriggerStore(runtime_paths).list_triggers(include_disabled=True),
        ]
        items = [item for rec in records if (item := _match_trigger_to_room(rec, room_id, runtime_paths))]
        items.sort(key=lambda item: (0 if item[1] == "target" else 1, float(item[0].created_at), item[0].trigger_id))
        return items

    matched = await _run_store(_load)
    ownership = [is_trigger_owner(room.user.user_id, rec.owner_user_id, config, runtime_paths) for rec, _, _ in matched]
    watch_targets = list(
        dict.fromkeys(
            target_room
            for (_, rel, target_room), owned in zip(matched, ownership, strict=True)
            if rel == "watch" and not owned
        ),
    )
    joined_targets = set(await client_rooms(request, watch_targets)) if watch_targets else set()
    return {
        "triggers": [
            _build_trigger_row(item, can_manage=owned, joined_target_rooms=joined_targets)
            for item, owned in zip(matched, ownership, strict=True)
        ],
    }


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
    record, relation, resolved_target = await _require_owned_room_trigger(
        runtime_paths,
        config,
        room_id,
        trigger_id,
        room.user.user_id,
    )

    refreshed_tag_present: bool | None = None
    if (
        isinstance(record, ReactiveTriggerRecord)
        and body.enabled
        and not record.enabled
        and isinstance(record.source, ThreadTagSourceSpec)
    ):
        bots = ensure_app_state(request.app).room_bots
        steward = None if bots is None else room_steward(bots, resolve_room_id(record.source.room_id, runtime_paths))
        if steward is None or steward.client is None:
            raise HTTPException(503, "No running MindRoom bot is in the watched room.")
        try:
            refreshed_tag_present = await read_thread_tag_present(steward.client, record.source)
        except ThreadTagsError as exc:
            raise HTTPException(503, "Watched thread tags could not be read; try again.") from exc

    updated = await _run_store(
        lambda: set_trigger_enabled_sync(
            runtime_paths,
            config,
            trigger_id,
            enabled=body.enabled,
            actor_user_id=room.user.user_id,
            refreshed_tag_present=refreshed_tag_present,
        ),
    )
    if body.enabled and isinstance(updated, ReactiveTriggerRecord):
        runtime = ensure_app_state(request.app).external_trigger_runtime
        if runtime is not None and runtime.on_reactive_event is not None:
            runtime.on_reactive_event()
    return _build_trigger_row((updated, relation, resolved_target), can_manage=True, joined_target_rooms=set())


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_room_trigger(request: Request, room: _Room, room_id: str, trigger_id: str) -> Response:
    """Delete one trigger belonging to ``room_id``; owner only."""
    config, runtime_paths = _require_trigger_runtime(request)
    await _require_owned_room_trigger(runtime_paths, config, room_id, trigger_id, room.user.user_id)
    await _run_store(lambda: delete_trigger_sync(runtime_paths, config, trigger_id, actor_user_id=room.user.user_id))
    return Response(status_code=204)


def _require_trigger_runtime(request: Request) -> tuple[Config, RuntimePaths]:
    try:
        snapshot = bind_current_request_snapshot(request)
    except TypeError as exc:
        raise HTTPException(503, "Trigger configuration is not available.") from exc
    if snapshot.runtime_config is None or snapshot.runtime_paths.control_state_root is None:
        raise HTTPException(503, "Trigger store is not available.")
    return snapshot.runtime_config, snapshot.runtime_paths


async def _run_store[T](call: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(call)
    except (ExternalTriggerStoreError, ReactiveTriggerStoreError, sqlite3.Error) as exc:
        raise HTTPException(503, "Trigger store is not available.") from exc


def _match_trigger_to_room(record: _TriggerRecord, room_id: str, runtime_paths: RuntimePaths) -> _MatchedTrigger | None:
    resolved_target = resolve_room_id(record.target.room_id, runtime_paths)
    if resolved_target == room_id:
        return (record, "target", resolved_target)
    if (
        isinstance(record, ReactiveTriggerRecord)
        and isinstance(record.source, (RoomMessageSourceSpec, ThreadTagSourceSpec))
        and resolve_room_id(record.source.room_id, runtime_paths) == room_id
    ):
        return (record, "watch", resolved_target)
    return None


async def _require_owned_room_trigger(
    runtime_paths: RuntimePaths,
    config: Config,
    room_id: str,
    trigger_id: str,
    user_id: str,
) -> _MatchedTrigger:
    def _find() -> _MatchedTrigger | None:
        record: _TriggerRecord | None = ExternalTriggerStore(runtime_paths).get_record(
            trigger_id,
        ) or ReactiveTriggerStore(runtime_paths).get_trigger(trigger_id)
        return None if record is None else _match_trigger_to_room(record, room_id, runtime_paths)

    matched = await _run_store(_find)
    if matched is None:
        raise HTTPException(404, "Trigger not found in this room.")
    if not is_trigger_owner(user_id, matched[0].owner_user_id, config, runtime_paths):
        raise HTTPException(403, "Only the trigger owner can manage this trigger.")
    return matched


def _build_trigger_row(matched: _MatchedTrigger, *, can_manage: bool, joined_target_rooms: set[str]) -> TriggerRow:
    record, relation, resolved_target = matched
    redacted = relation == "watch" and not can_manage and resolved_target not in joined_target_rooms
    target = record.target
    thread_mode: _TriggerThreadMode = (
        "new_thread"
        if target.new_thread
        else "origin"
        if target.thread_id == "origin"
        else "thread"
        if target.thread_id is not None
        else "room"
    )
    target_thread_id = target.thread_id if thread_mode == "thread" else None

    if isinstance(record, ExternalTriggerRecord):
        kind: _TriggerKind = "webhook"
        prompt: str | None = None
        disabled_reason: str | None = None if record.enabled else "manual"
        once, expires_at = record.delivery_mode == "single_use", None
    else:
        kind = record.source.source_type
        prompt = None if redacted else record.target.prompt
        disabled_reason = record.disabled_reason
        once = record.once
        expires_at = None if record.expires_at is None else int(record.expires_at * 1000)

    return TriggerRow(
        trigger_id=record.trigger_id,
        kind=kind,
        relation=relation,
        description="" if redacted else record.description,
        prompt=prompt,
        agent=None if redacted else target.agent,
        target_room_id=None if redacted else resolved_target,
        target_thread_id=None if redacted else target_thread_id,
        thread_mode=thread_mode,
        enabled=record.enabled,
        disabled_reason=disabled_reason,
        once=once,
        expires_at=expires_at,
        last_fired_at=None if record.last_fired_at is None else int(record.last_fired_at * 1000),
        owner_user_id=record.owner_user_id,
        can_manage=can_manage,
    )
