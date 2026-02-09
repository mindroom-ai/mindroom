"""API endpoints for scheduled task management."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from croniter import CroniterError, croniter  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from mindroom.constants import MATRIX_HOMESERVER, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.rooms import get_room_alias_from_id, resolve_room_aliases
from mindroom.matrix.users import create_agent_user, login_agent_user
from mindroom.scheduling import (
    SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR,
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    cancel_scheduled_task,
    get_scheduled_task,
    get_scheduled_tasks_for_room,
    save_edited_scheduled_task,
)

if TYPE_CHECKING:
    from nio import AsyncClient

    from mindroom.config import Config

router = APIRouter(prefix="/api/schedules", tags=["schedules"])
logger = get_logger(__name__)


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


def _resolve_room_id(room_id_or_alias: str) -> str:
    """Resolve room aliases (e.g. lobby) to room IDs when available."""
    resolved = resolve_room_aliases([room_id_or_alias])
    return resolved[0] if resolved else room_id_or_alias


def _configured_room_ids(runtime_config: Config) -> list[str]:
    """Return configured rooms resolved to Matrix room IDs."""
    configured_rooms = sorted(runtime_config.get_all_configured_rooms())
    resolved_rooms = resolve_room_aliases(configured_rooms)
    # Keep order while de-duplicating
    return list(dict.fromkeys(resolved_rooms))


def _cron_schedule_from_expression(cron_expression: str) -> CronSchedule:
    """Convert and validate a cron expression into a CronSchedule."""
    raw_expression = cron_expression.strip()
    fields = raw_expression.split()
    if len(fields) != 5:
        msg = "Cron expression must have exactly 5 fields: minute hour day month weekday"
        raise ValueError(msg)

    # Validate expression syntax
    croniter(raw_expression, datetime.now(UTC))
    minute, hour, day, month, weekday = fields
    return CronSchedule(minute=minute, hour=hour, day=day, month=month, weekday=weekday)


def _to_response_task(task: ScheduledTaskRecord) -> ScheduledTaskResponse:
    """Map an internal scheduled task record to the API response model."""
    workflow = task.workflow
    cron_expression = workflow.cron_schedule.to_cron_string() if workflow.cron_schedule else None
    cron_description = workflow.cron_schedule.to_natural_language() if workflow.cron_schedule else None

    next_run_at: datetime | None = None
    if workflow.schedule_type == "once":
        next_run_at = workflow.execute_at
    elif cron_expression:
        try:
            next_run_at = croniter(cron_expression, datetime.now(UTC)).get_next(datetime)
        except CroniterError:
            logger.warning(
                "Failed to compute next run time for scheduled task",
                task_id=task.task_id,
                cron_expression=cron_expression,
            )

    return ScheduledTaskResponse(
        task_id=task.task_id,
        room_id=task.room_id,
        room_alias=get_room_alias_from_id(task.room_id),
        status=task.status,
        schedule_type=workflow.schedule_type,
        execute_at=workflow.execute_at,
        next_run_at=next_run_at,
        cron_expression=cron_expression,
        cron_description=cron_description,
        description=workflow.description,
        message=workflow.message,
        thread_id=workflow.thread_id,
        created_by=workflow.created_by,
        created_at=task.created_at,
    )


def _task_sort_key(task: ScheduledTaskResponse) -> tuple[int, datetime]:
    """Sort pending tasks first, then by next execution time."""
    status_rank = 0 if task.status == "pending" else 1
    scheduled_time = task.next_run_at or datetime.max.replace(tzinfo=UTC)
    return (status_rank, scheduled_time)


def _resolve_schedule_fields(
    request: UpdateScheduleRequest,
    existing_workflow: ScheduledWorkflow,
) -> tuple[Literal["once", "cron"], datetime | None, CronSchedule | None]:
    """Resolve and validate schedule-related updates for a task edit."""
    if request.schedule_type and request.schedule_type != existing_workflow.schedule_type:
        raise HTTPException(
            status_code=400,
            detail=SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR,
        )

    schedule_type = existing_workflow.schedule_type
    if schedule_type == "once":
        if request.cron_expression is not None:
            raise HTTPException(status_code=400, detail="cron_expression is only valid for cron schedules")
        execute_at = request.execute_at or existing_workflow.execute_at
        if execute_at is None:
            raise HTTPException(status_code=400, detail="execute_at is required for one-time schedules")
        return (schedule_type, execute_at, None)

    if request.execute_at is not None:
        raise HTTPException(status_code=400, detail="execute_at is only valid for one-time schedules")

    cron_schedule = existing_workflow.cron_schedule
    if request.cron_expression is not None:
        try:
            cron_schedule = _cron_schedule_from_expression(request.cron_expression)
        except (ValueError, CroniterError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e!s}") from e
    if cron_schedule is None:
        raise HTTPException(status_code=400, detail="cron_expression is required for cron schedules")
    return (schedule_type, None, cron_schedule)


def _resolve_message_fields(
    request: UpdateScheduleRequest,
    existing_workflow: ScheduledWorkflow,
) -> tuple[str, str]:
    """Resolve and validate message/description fields for a task edit."""
    message_source = request.message if request.message is not None else existing_workflow.message
    message = message_source.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    description_source = request.description if request.description is not None else existing_workflow.description
    description = description_source.strip() or message
    return (message, description)


def _build_updated_workflow(
    request: UpdateScheduleRequest,
    existing_workflow: ScheduledWorkflow,
    resolved_room_id: str,
) -> ScheduledWorkflow:
    """Build an updated workflow from validated edit inputs."""
    schedule_type, execute_at, cron_schedule = _resolve_schedule_fields(request, existing_workflow)
    message, description = _resolve_message_fields(request, existing_workflow)
    return ScheduledWorkflow(
        schedule_type=schedule_type,
        execute_at=execute_at,
        cron_schedule=cron_schedule,
        message=message,
        description=description,
        created_by=existing_workflow.created_by,
        thread_id=existing_workflow.thread_id,
        room_id=resolved_room_id,
    )


async def _get_router_client() -> AsyncClient:
    """Login the router user and return an authenticated Matrix client."""
    router_user = await create_agent_user(
        MATRIX_HOMESERVER,
        ROUTER_AGENT_NAME,
        "RouterAgent",
    )
    return await login_agent_user(MATRIX_HOMESERVER, router_user)


@router.get("", response_model=ListSchedulesResponse)
async def list_schedules(
    room_id: RoomFilter = None,
    include_cancelled: IncludeCancelled = False,
) -> ListSchedulesResponse:
    """List scheduled tasks from one room or all configured rooms."""
    from .main import load_runtime_config  # noqa: PLC0415

    runtime_config, _ = load_runtime_config()
    room_ids = [_resolve_room_id(room_id)] if room_id else _configured_room_ids(runtime_config)

    if not room_ids:
        return ListSchedulesResponse(timezone=runtime_config.timezone, tasks=[])

    client = await _get_router_client()
    try:
        tasks: list[ScheduledTaskResponse] = []
        for resolved_room_id in room_ids:
            room_tasks = await get_scheduled_tasks_for_room(
                client=client,
                room_id=resolved_room_id,
                include_non_pending=include_cancelled,
            )
            tasks.extend(_to_response_task(task) for task in room_tasks)
    finally:
        await client.close()

    tasks.sort(key=_task_sort_key)
    return ListSchedulesResponse(timezone=runtime_config.timezone, tasks=tasks)


@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_schedule(
    task_id: str,
    request: UpdateScheduleRequest,
) -> ScheduledTaskResponse:
    """Update prompt text and schedule fields for an existing task."""
    from .main import load_runtime_config  # noqa: PLC0415

    runtime_config, _ = load_runtime_config()
    resolved_room_id = _resolve_room_id(request.room_id)

    client = await _get_router_client()
    try:
        existing_task = await get_scheduled_task(client=client, room_id=resolved_room_id, task_id=task_id)
        if not existing_task:
            raise HTTPException(status_code=404, detail=f"Task `{task_id}` not found")

        updated_workflow = _build_updated_workflow(request, existing_task.workflow, resolved_room_id)
        try:
            updated_task = await save_edited_scheduled_task(
                client=client,
                room_id=resolved_room_id,
                task_id=task_id,
                workflow=updated_workflow,
                config=runtime_config,
                existing_task=existing_task,
                restart_task=False,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{e!s}") from e

        return _to_response_task(updated_task)
    finally:
        await client.close()


@router.delete("/{task_id}", response_model=CancelScheduleResponse)
async def cancel_schedule(
    task_id: str,
    room_id: CancelRoomId,
) -> CancelScheduleResponse:
    """Cancel a scheduled task by ID."""
    resolved_room_id = _resolve_room_id(room_id)

    client = await _get_router_client()
    try:
        existing = await get_scheduled_task(client=client, room_id=resolved_room_id, task_id=task_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Task `{task_id}` not found")

        await cancel_scheduled_task(
            client=client,
            room_id=resolved_room_id,
            task_id=task_id,
            cancel_in_memory=False,
        )
    finally:
        await client.close()

    return CancelScheduleResponse(success=True, message=f"Cancelled task `{task_id}`")
