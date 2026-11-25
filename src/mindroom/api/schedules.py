"""API endpoints for scheduled task management."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

import aiohttp
import nio
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.matrix.client_room_admin import resolve_room_reference
from mindroom.matrix.state import get_room_alias_from_id, resolve_room_id
from mindroom.scheduling.errors import (
    ScheduledTaskNotFoundError,
    ScheduledTaskStateReadError,
    ScheduledTaskStorageError,
    SchedulingError,
)
from mindroom.scheduling.tasks import (
    build_edited_scheduled_workflow,
    cancel_scheduled_task,
    get_scheduled_tasks_for_room,
    require_scheduled_task,
    save_edited_scheduled_task,
    scheduled_task_timezone,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.scheduling.tasks import ScheduledTaskRecord

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduledTaskResponse(BaseModel):
    """UI-friendly scheduled task payload."""

    task_id: str
    room_id: str
    room_alias: str | None = None
    status: str
    schedule_type: Literal["once", "cron"]
    execute_at: datetime | None = None
    next_run_at: datetime | None = None
    cron_expression: str | None = None
    cron_description: str | None = None
    timezone: str
    cron_timezone: str | None = None
    description: str
    message: str
    history_limit: int | None = None
    thread_id: str | None = None
    new_thread: bool
    silent: bool
    created_by: str | None = None
    created_at: datetime | None = None


class UnreadableScheduleRoom(BaseModel):
    """A room whose scheduled tasks could not be read, so the listing leaves them out."""

    room_id: str
    room_alias: str | None = None
    error: str


class ListSchedulesResponse(BaseModel):
    """Response for listing schedules."""

    timezone: str
    tasks: list[ScheduledTaskResponse]
    unreadable_rooms: list[UnreadableScheduleRoom]


class UpdateScheduleRequest(BaseModel):
    """Patch-like request for updating a scheduled task."""

    room_id: str = Field(description="Room ID or alias where the task is stored")
    message: str | None = None
    description: str | None = None
    schedule_type: Literal["once", "cron"] | None = None
    execute_at: datetime | None = None
    cron_expression: str | None = None
    silent: bool | None = None


class CancelScheduleResponse(BaseModel):
    """Response for cancelling a scheduled task."""

    success: bool
    message: str


RoomFilter = Annotated[str | None, Query(description="Optional room ID or alias filter")]
IncludeCancelled = Annotated[bool, Query(description="Include cancelled schedules in the result")]
CancelRoomId = Annotated[str, Query(description="Room ID or alias containing the task")]


def _unresolved_alias(alias: str, reason: object) -> UnreadableScheduleRoom:
    """Report an alias the homeserver did not resolve under the alias itself, since no room ID is known."""
    return UnreadableScheduleRoom(room_id=alias, room_alias=alias, error=f"Unable to resolve room alias: {reason}")


@contextmanager
def _room_client(request: Request, room_id: str) -> Iterator[nio.AsyncClient]:
    """Lend the live client of the room's steward for one request, which a stop of that bot waits for.

    Raises that the room cannot be read right now where no bot that joined it runs, or its only ones are stopping.
    """
    with config_lifecycle.app_state(request.app).runtime.scheduled_tasks.lend_client(room_id) as client:
        if client is None:
            msg = f"Unable to retrieve scheduled tasks: no agent that joined room `{room_id}` is running right now"
            raise ScheduledTaskStateReadError(msg)
        yield client


async def _room_ids(
    request: Request,
    room_refs: list[str],
    runtime_paths: RuntimePaths,
) -> tuple[list[str], list[UnreadableScheduleRoom]]:
    """Return the Matrix room IDs that room references name, and each alias the homeserver cannot resolve.

    A room key without a recorded room ID names a room not created yet, which holds no tasks.
    Any running bot's client looks an alias up, since the room it names is not known yet.
    """
    room_ids: list[str] = []
    unresolved_rooms: list[UnreadableScheduleRoom] = []
    with config_lifecycle.app_state(request.app).runtime.scheduled_tasks.lend_client() as client:
        if client is None:
            # With no bot running nothing looks an alias up; the rooms MindRoom recorded are reported by their reader.
            recorded = [(room_ref, resolve_room_id(room_ref, runtime_paths)) for room_ref in room_refs]
            return (
                list(dict.fromkeys(room_id for _ref, room_id in recorded if room_id.startswith("!"))),
                [_unresolved_alias(ref, "no agent is running") for ref, room_id in recorded if room_id.startswith("#")],
            )
        for room_ref in room_refs:
            try:
                resolved = await resolve_room_reference(client, room_ref, runtime_paths)
            except (aiohttp.ClientError, nio.LocalProtocolError) as exc:
                # nio raises an alias lookup whose response broke off, or whose lent client its bot closed since.
                unresolved_rooms.append(_unresolved_alias(room_ref, f"{type(exc).__name__}: {exc}"))
                continue
            if isinstance(resolved, str):
                room_ids.append(resolved)
            elif resolved is not None:
                unresolved_rooms.append(_unresolved_alias(room_ref, resolved))
    # Two references can name one room, such as its room key and its full alias.
    return list(dict.fromkeys(room_ids)), unresolved_rooms


def _http_error(error: SchedulingError) -> HTTPException:
    """Answer a missing task with 404, failed task storage with 500, and any other refused request with 400."""
    if isinstance(error, ScheduledTaskNotFoundError):
        status_code = 404
    elif isinstance(error, ScheduledTaskStorageError):
        status_code = 500
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=str(error))


def _task_response(
    task: ScheduledTaskRecord,
    *,
    timezone: str,
    runtime_paths: RuntimePaths,
    current_time: datetime | None = None,
) -> ScheduledTaskResponse:
    """Build the API response payload for a stored scheduled task."""
    workflow = task.workflow
    return ScheduledTaskResponse(
        task_id=task.task_id,
        room_id=task.room_id,
        room_alias=get_room_alias_from_id(task.room_id, runtime_paths=runtime_paths),
        status=task.status,
        schedule_type=workflow.schedule_type,
        execute_at=workflow.execute_at,
        next_run_at=workflow.next_run_at(current_time),
        cron_expression=workflow.cron_schedule.to_cron_string() if workflow.cron_schedule else None,
        cron_description=workflow.cron_description(timezone, current_time),
        timezone=timezone,
        cron_timezone=workflow.cron_timezone,
        description=workflow.description,
        message=workflow.message,
        history_limit=workflow.history_limit,
        thread_id=workflow.thread_id,
        new_thread=workflow.new_thread,
        silent=workflow.silent,
        created_by=workflow.created_by,
        created_at=task.created_at,
    )


def _task_response_sort_key(task: ScheduledTaskResponse) -> tuple[int, datetime]:
    """Sort pending tasks first, then by next execution time."""
    return (0 if task.status == "pending" else 1, task.next_run_at or datetime.max.replace(tzinfo=UTC))


@router.get("", response_model=ListSchedulesResponse)
async def list_schedules(
    request: Request,
    room_id: RoomFilter = None,
    include_cancelled: IncludeCancelled = False,
) -> ListSchedulesResponse:
    """List scheduled tasks from one room or all configured rooms."""
    runtime_config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    room_refs = [room_id] if room_id else sorted(runtime_config.get_all_configured_rooms())

    if not room_refs:
        return ListSchedulesResponse(timezone=runtime_config.timezone, tasks=[], unreadable_rooms=[])

    tasks: list[ScheduledTaskResponse] = []
    # A room that stays unreadable must neither look empty nor hide every other room's tasks.
    room_ids, unreadable_rooms = await _room_ids(request, room_refs, runtime_paths)
    for resolved_room_id in room_ids:
        try:
            with _room_client(request, resolved_room_id) as client:
                room_tasks = await get_scheduled_tasks_for_room(
                    client=client,
                    room_id=resolved_room_id,
                    runtime_paths=runtime_paths,
                    include_non_pending=include_cancelled,
                )
        except ScheduledTaskStateReadError as e:
            unreadable_rooms.append(
                UnreadableScheduleRoom(
                    room_id=resolved_room_id,
                    room_alias=get_room_alias_from_id(resolved_room_id, runtime_paths=runtime_paths),
                    error=str(e),
                ),
            )
        else:
            tasks.extend(
                _task_response(
                    task,
                    timezone=scheduled_task_timezone(task.workflow, runtime_config),
                    runtime_paths=runtime_paths,
                )
                for task in room_tasks
            )

    tasks.sort(key=_task_response_sort_key)
    return ListSchedulesResponse(
        timezone=runtime_config.timezone,
        tasks=tasks,
        unreadable_rooms=unreadable_rooms,
    )


@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_schedule(
    task_id: str,
    request: UpdateScheduleRequest,
    api_request: Request,
) -> ScheduledTaskResponse:
    """Update prompt text and schedule fields for an existing task."""
    runtime_config, runtime_paths = config_lifecycle.read_committed_runtime_config(api_request)
    resolved_room_id = resolve_room_id(request.room_id, runtime_paths=runtime_paths)

    try:
        with _room_client(api_request, resolved_room_id) as client:
            existing_task = await require_scheduled_task(client, resolved_room_id, task_id, runtime_paths)
            updated_workflow = build_edited_scheduled_workflow(
                existing_task.workflow,
                room_id=resolved_room_id,
                message=request.message,
                description=request.description,
                schedule_type=request.schedule_type,
                execute_at=request.execute_at,
                cron_expression=request.cron_expression,
                silent=request.silent,
            )
            updated_task = await save_edited_scheduled_task(
                client=client,
                workflow=updated_workflow,
                existing_task=existing_task,
                runtime_paths=runtime_paths,
                config=runtime_config,
                writers=config_lifecycle.app_state(api_request.app).runtime.scheduled_tasks,
            )
    except SchedulingError as e:
        raise _http_error(e) from e

    return _task_response(
        updated_task,
        timezone=scheduled_task_timezone(updated_task.workflow, runtime_config),
        runtime_paths=runtime_paths,
    )


@router.delete("/{task_id}", response_model=CancelScheduleResponse)
async def cancel_schedule(
    task_id: str,
    request: Request,
    room_id: CancelRoomId,
) -> CancelScheduleResponse:
    """Cancel a scheduled task by ID."""
    runtime_paths = api_runtime_paths(request)
    resolved_room_id = resolve_room_id(room_id, runtime_paths=runtime_paths)

    try:
        with _room_client(request, resolved_room_id) as client:
            await cancel_scheduled_task(
                client=client,
                room_id=resolved_room_id,
                task_id=task_id,
                runtime_paths=runtime_paths,
                task_runtime=config_lifecycle.app_state(request.app).runtime.scheduled_tasks,
            )
    except SchedulingError as e:
        raise _http_error(e) from e

    return CancelScheduleResponse(success=True, message=f"Cancelled task `{task_id}`")
