"""Chat clients' room-scoped read and owner-only toggle/delete API for triggers."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from mindroom.api.app_state import notify_reactive_event_loop
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.api.room_reads import read_in_room
from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerStore, ExternalTriggerStoreError
from mindroom.matrix.state import resolve_room_id
from mindroom.plugin_http import ClientUser, client_rooms, require_room_member
from mindroom.reactive_triggers.management import (
    TriggerRecord,
    delete_trigger_sync,
    get_trigger_record,
    is_trigger_owner,
    read_thread_tag_present,
    set_trigger_enabled_sync,
)
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    RoomMessageSourceSpec,
    ScriptCheckSourceSpec,
    SourceType,
    ThreadTagSourceSpec,
)
from mindroom.reactive_triggers.script_check import check_script_check_availability
from mindroom.reactive_triggers.store import (
    ReactiveTriggerLimitError,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
)
from mindroom.requester_identity import equivalent_requester_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.external_triggers.store import ExternalTriggerTarget
    from mindroom.reactive_triggers.models import TriggerTargetSpec

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["room-triggers"])

_Member = Annotated[ClientUser, Depends(require_room_member)]
_TriggerKind = Literal["webhook", SourceType]
_TriggerRelation = Literal["target", "watch"]
_TriggerThreadMode = Literal["room", "thread", "new_thread", "origin"]


@dataclass(frozen=True)
class _MatchedTrigger:
    record: TriggerRecord
    relation: _TriggerRelation
    target_room_id: str


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
    reopen_on_fire: bool
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
async def list_room_triggers(request: Request, member: _Member, room_id: str) -> dict[str, list[TriggerRow]]:
    """Return triggers targeting or watching ``room_id`` with cross-room watch redaction."""
    config, runtime_paths = _require_trigger_runtime(request)

    def _load() -> list[_MatchedTrigger]:
        records: list[TriggerRecord] = [
            *ExternalTriggerStore(runtime_paths).list_records(),
            *ReactiveTriggerStore(runtime_paths).list_triggers(include_disabled=True),
        ]
        items = [item for rec in records if (item := _match_trigger_to_room(rec, room_id, runtime_paths))]
        items.sort(
            key=lambda item: (
                0 if item.relation == "target" else 1,
                float(item.record.created_at),
                item.record.trigger_id,
            ),
        )
        return items

    matched = await _run_store(_load)
    owner_ids = equivalent_requester_ids(member.user_id, config, runtime_paths)
    watch_targets = list(
        dict.fromkeys(
            item.target_room_id
            for item in matched
            if item.relation == "watch" and item.record.owner_user_id not in owner_ids
        ),
    )
    joined_targets = set(await client_rooms(request, watch_targets)) if watch_targets else set()
    return {
        "triggers": [
            _build_trigger_row(
                item,
                can_manage=item.record.owner_user_id in owner_ids,
                joined_target_rooms=joined_targets,
            )
            for item in matched
        ],
    }


@router.patch("/triggers/{trigger_id}")
async def update_room_trigger(
    request: Request,
    member: _Member,
    room_id: str,
    trigger_id: str,
    body: UpdateRoomTriggerRequest,
) -> TriggerRow:
    """Enable or disable one trigger belonging to ``room_id``; owner only."""
    config, runtime_paths = _require_trigger_runtime(request)
    matched = await _require_owned_room_trigger(runtime_paths, config, room_id, trigger_id, member.user_id)
    record = matched.record

    refreshed_tag_present: bool | None = None
    if isinstance(record, ReactiveTriggerRecord) and body.enabled and not record.enabled:
        if isinstance(source := record.source, ThreadTagSourceSpec):
            refreshed_tag_present = await read_in_room(
                request.app,
                resolve_room_id(source.room_id, runtime_paths),
                lambda client: read_thread_tag_present(client, source),
                "Watched thread tags could not be read; try again.",
            )
        elif isinstance(source, ScriptCheckSourceSpec) and (
            failure := check_script_check_availability(config, runtime_paths, source.agent)
        ):
            kind, message = failure
            raise HTTPException(409 if kind == "placement" else 403, message)

    updated = await _run_store(
        lambda: set_trigger_enabled_sync(
            runtime_paths,
            config,
            trigger_id,
            enabled=body.enabled,
            actor_user_id=member.user_id,
            authorize_reactive=partial(_require_owner, member.user_id, config, runtime_paths),
            refreshed_tag_present=refreshed_tag_present,
        ),
    )
    if body.enabled and isinstance(updated, ReactiveTriggerRecord):
        notify_reactive_event_loop(request.app, trigger_id=trigger_id)
    return _build_trigger_row(
        _MatchedTrigger(updated, matched.relation, matched.target_room_id),
        can_manage=True,
        joined_target_rooms=set(),
    )


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_room_trigger(request: Request, member: _Member, room_id: str, trigger_id: str) -> Response:
    """Delete one trigger belonging to ``room_id``; owner only."""
    config, runtime_paths = _require_trigger_runtime(request)
    await _require_owned_room_trigger(runtime_paths, config, room_id, trigger_id, member.user_id)
    await _run_store(
        lambda: delete_trigger_sync(
            runtime_paths,
            config,
            trigger_id,
            actor_user_id=member.user_id,
            authorize_reactive=partial(_require_owner, member.user_id, config, runtime_paths),
        ),
    )
    return Response(status_code=204)


def _require_trigger_runtime(request: Request) -> tuple[Config, RuntimePaths]:
    snapshot = bind_current_request_snapshot(request)
    if snapshot.runtime_config is None:
        raise HTTPException(503, "Trigger store is not available.")
    return snapshot.runtime_config, snapshot.runtime_paths


def _require_owner(user_id: str, config: Config, runtime_paths: RuntimePaths, record: TriggerRecord) -> None:
    if not is_trigger_owner(user_id, record.owner_user_id, config, runtime_paths):
        raise HTTPException(403, "Only the trigger owner can manage this trigger.")


async def _run_store[T](call: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(call)
    except ReactiveTriggerLimitError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ExternalTriggerStoreError, ReactiveTriggerStoreError, sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Trigger store is not available.") from exc


def _match_trigger_to_room(record: TriggerRecord, room_id: str, runtime_paths: RuntimePaths) -> _MatchedTrigger | None:
    resolved_target = resolve_room_id(record.target.room_id, runtime_paths)
    if resolved_target == room_id:
        return _MatchedTrigger(record, "target", resolved_target)
    if (
        isinstance(record, ReactiveTriggerRecord)
        and isinstance(record.source, (RoomMessageSourceSpec, ThreadTagSourceSpec))
        and resolve_room_id(record.source.room_id, runtime_paths) == room_id
    ):
        return _MatchedTrigger(record, "watch", resolved_target)
    return None


async def _require_owned_room_trigger(
    runtime_paths: RuntimePaths,
    config: Config,
    room_id: str,
    trigger_id: str,
    user_id: str,
) -> _MatchedTrigger:
    def _find() -> _MatchedTrigger | None:
        record = get_trigger_record(runtime_paths, trigger_id)
        return None if record is None else _match_trigger_to_room(record, room_id, runtime_paths)

    matched = await _run_store(_find)
    if matched is None:
        raise HTTPException(404, "Trigger not found in this room.")
    _require_owner(user_id, config, runtime_paths, matched.record)
    return matched


def _thread_mode(target: ExternalTriggerTarget | TriggerTargetSpec) -> _TriggerThreadMode:
    if target.new_thread:
        return "new_thread"
    if target.thread_id == "origin":
        return "origin"
    if target.thread_id is not None:
        return "thread"
    return "room"


def _build_trigger_row(matched: _MatchedTrigger, *, can_manage: bool, joined_target_rooms: set[str]) -> TriggerRow:
    record = matched.record
    redacted = matched.relation == "watch" and not can_manage and matched.target_room_id not in joined_target_rooms
    target = record.target
    thread_mode = _thread_mode(target)
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
        relation=matched.relation,
        description="" if redacted else record.description,
        prompt=prompt,
        agent=None if redacted else target.agent,
        target_room_id=None if redacted else matched.target_room_id,
        target_thread_id=None if redacted else target_thread_id,
        thread_mode=thread_mode,
        reopen_on_fire=False if redacted else target.reopen_on_fire,
        enabled=record.enabled,
        disabled_reason=disabled_reason,
        once=once,
        expires_at=expires_at,
        last_fired_at=None if record.last_fired_at is None else int(record.last_fired_at * 1000),
        owner_user_id=record.owner_user_id,
        can_manage=can_manage,
    )
