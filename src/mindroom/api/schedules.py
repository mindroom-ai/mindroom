"""API endpoints for scheduled task management."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime  # noqa: TC003 - Pydantic resolves postponed annotations at runtime.
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.matrix.state import get_room_alias_from_id, resolve_room_aliases, resolve_room_id
from mindroom.matrix.users import create_agent_user, login_agent_user
from mindroom.scheduling import (
    ScheduledTaskReadModel,
    ScheduledTaskRecord,
    build_edited_scheduled_workflow,
    build_scheduled_task_read_model,
    cancel_scheduled_task,
    get_scheduled_task,
    get_scheduled_tasks_for_room,
    save_edited_scheduled_task,
    scheduled_task_read_sort_key,
)

if TYPE_CHECKING:
    from nio import AsyncClient

    from mindroom.config.main import Config

router = APIRouter(prefix="/api/schedules", tags=["schedules"])
_SCHEDULER_ERROR_PREFIX = "❌ "


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
    description: str
    message: str
    thread_id: str | None = None
    new_thread: bool
    created_by: str | None = None
    created_at: datetime | None = None


class ListSchedulesResponse(BaseModel):
    """Response for listing schedules."""

    timezone: str
    tasks: list[ScheduledTaskResponse]


class UpdateScheduleRequest(BaseModel):
    """Patch-like request for updating a scheduled task."""

    room_id: str = Field(description="Room ID or alias where the task is stored")
    message: str | None = None
    description: str | None = None
    schedule_type: Literal["once", "cron"] | None = None
    execute_at: datetime | None = None
    cron_expression: str | None = None


class CancelScheduleResponse(BaseModel):
    """Response for cancelling a scheduled task."""

    success: bool
    message: str


RoomFilter = Annotated[str | None, Query(description="Optional room ID or alias filter")]
IncludeCancelled = Annotated[bool, Query(description="Include cancelled schedules in the result")]
CancelRoomId = Annotated[str, Query(description="Room ID or alias containing the task")]


def _configured_room_ids(runtime_config: Config, runtime_paths: RuntimePaths) -> list[str]:
    """Return configured rooms resolved to Matrix room IDs."""
    configured_rooms = sorted(runtime_config.get_all_configured_rooms())
    resolved_rooms = resolve_room_aliases(configured_rooms, runtime_paths=runtime_paths)
    # Keep order while de-duplicating
    return list(dict.fromkeys(resolved_rooms))


def _to_response_task(task: ScheduledTaskReadModel, runtime_paths: RuntimePaths) -> ScheduledTaskResponse:
    return ScheduledTaskResponse(
        room_alias=get_room_alias_from_id(task.room_id, runtime_paths=runtime_paths),
        **asdict(task),
    )


def _scheduler_error_detail(result: str) -> str:
    """Convert chat-formatted scheduler errors into API response details."""
    return result.removeprefix(_SCHEDULER_ERROR_PREFIX).strip()


def _cancel_error_status_code(detail: str) -> int:
    """Return the HTTP status for one scheduler cancel failure detail."""
    if detail.startswith("Task `") and detail.endswith("` not found."):
        return 404
    return 500


async def _get_router_client(runtime_paths: RuntimePaths) -> AsyncClient:
    """Login the router user and return an authenticated Matrix client."""
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    router_user = await create_agent_user(
        homeserver,
        ROUTER_AGENT_NAME,
        "RouterAgent",
        runtime_paths=runtime_paths,
    )
    return await login_agent_user(homeserver, router_user, runtime_paths)


@router.get("", response_model=ListSchedulesResponse)
async def list_schedules(
    request: Request,
    room_id: RoomFilter = None,
    include_cancelled: IncludeCancelled = False,
) -> ListSchedulesResponse:
    """List scheduled tasks from one room or all configured rooms."""
    runtime_config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    room_ids = (
        [resolve_room_id(room_id, runtime_paths=runtime_paths)]
        if room_id
        else _configured_room_ids(runtime_config, runtime_paths=runtime_paths)
    )

    if not room_ids:
        return ListSchedulesResponse(timezone=runtime_config.timezone, tasks=[])

    client = await _get_router_client(runtime_paths)
    try:
        tasks: list[ScheduledTaskReadModel] = []
        for resolved_room_id in room_ids:
            room_tasks: list[ScheduledTaskRecord] = await get_scheduled_tasks_for_room(
                client=client,
                room_id=resolved_room_id,
                include_non_pending=include_cancelled,
            )
            tasks.extend(build_scheduled_task_read_model(task) for task in room_tasks)
    finally:
        await client.close()

    tasks.sort(key=scheduled_task_read_sort_key)
    return ListSchedulesResponse(
        timezone=runtime_config.timezone,
        tasks=[_to_response_task(task, runtime_paths) for task in tasks],
    )


@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_schedule(
    task_id: str,
    request: UpdateScheduleRequest,
    api_request: Request,
) -> ScheduledTaskResponse:
    """Update prompt text and schedule fields for an existing task."""
    _, runtime_paths = config_lifecycle.read_committed_runtime_config(api_request)
    resolved_room_id = resolve_room_id(request.room_id, runtime_paths=runtime_paths)

    client = await _get_router_client(runtime_paths)
    try:
        existing_task = await get_scheduled_task(client=client, room_id=resolved_room_id, task_id=task_id)
        if not existing_task:
            raise HTTPException(status_code=404, detail=f"Task `{task_id}` not found")

        try:
            updated_workflow = build_edited_scheduled_workflow(
                existing_task.workflow,
                room_id=resolved_room_id,
                message=request.message,
                description=request.description,
                schedule_type=request.schedule_type,
                execute_at=request.execute_at,
                cron_expression=request.cron_expression,
            )
            updated_task = await save_edited_scheduled_task(
                client=client,
                room_id=resolved_room_id,
                task_id=task_id,
                workflow=updated_workflow,
                existing_task=existing_task,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{e!s}") from e

        return _to_response_task(build_scheduled_task_read_model(updated_task), runtime_paths)
    finally:
        await client.close()


@router.delete("/{task_id}", response_model=CancelScheduleResponse)
async def cancel_schedule(
    task_id: str,
    request: Request,
    room_id: CancelRoomId,
) -> CancelScheduleResponse:
    """Cancel a scheduled task by ID."""
    runtime_paths = api_runtime_paths(request)
    resolved_room_id = resolve_room_id(room_id, runtime_paths=runtime_paths)

    client = await _get_router_client(runtime_paths)
    try:
        existing = await get_scheduled_task(client=client, room_id=resolved_room_id, task_id=task_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Task `{task_id}` not found")

        result = await cancel_scheduled_task(
            client=client,
            room_id=resolved_room_id,
            task_id=task_id,
            cancel_in_memory=False,
        )
        if result.startswith("❌"):
            detail = _scheduler_error_detail(result)
            raise HTTPException(status_code=_cancel_error_status_code(detail), detail=detail)
    finally:
        await client.close()

    return CancelScheduleResponse(success=True, message=f"Cancelled task `{task_id}`")
