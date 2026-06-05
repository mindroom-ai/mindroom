"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import typing
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple
from zoneinfo import ZoneInfo

import humanize
import nio
from agno.agent import Agent
from cron_descriptor import Options, get_description
from croniter import CroniterError, croniter
from pydantic import BaseModel, Field

from mindroom import model_loading
from mindroom.authorization import responder_candidate_entities_for_room
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import (
    EVENT_SCHEDULE_FIRED,
    HookRegistry,
    ScheduleFiredContext,
    build_hook_matrix_admin,
    build_hook_message_sender,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
    send_and_track_message,
)
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from mindroom.matrix.message_builder import build_message_content
from mindroom.message_target import MessageTarget
from mindroom.thread_utils import filter_thread_agents_for_sender, get_agents_in_thread

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol, ConversationEventCache

logger = get_logger(__name__)

# Event type for scheduled tasks in Matrix state
_SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"

# Maximum length for message preview in task listings
_MESSAGE_PREVIEW_LENGTH = 50

# Shared validation message for edit attempts that change task type.
_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR = "Changing schedule_type is not supported; cancel and recreate the schedule"

# How often running tasks should re-check persisted Matrix state for edits/cancellations.
_TASK_STATE_POLL_INTERVAL_SECONDS = 30

# Maximum age (in seconds) for a missed one-time task to still be executed on restart.
# Tasks older than this are marked as failed instead of executed.
_MISSED_TASK_MAX_AGE_SECONDS = 86400  # 24 hours

# Small pause between draining overdue one-time tasks after sync is ready.
_DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS = 0.25

# Global task storage for running asyncio tasks
_running_tasks: dict[str, asyncio.Task] = {}
_deferred_overdue_tasks: deque[_DeferredOverdueTaskStart] = deque()
_deferred_overdue_task_ids: set[str] = set()
_ACTIVE_HOOK_REGISTRY: HookRegistry = HookRegistry.empty()


class _AgentValidationResult(NamedTuple):
    """Result of agent mention validation."""

    all_valid: bool
    valid_agents: list[MatrixID]
    invalid_agents: list[MatrixID]


def _raise_scheduled_workflow_send_error() -> typing.NoReturn:
    """Raise when a scheduled workflow message cannot be sent."""
    msg = "Failed to send scheduled workflow message to Matrix"
    raise RuntimeError(msg)


def set_scheduling_hook_registry(hook_registry: HookRegistry) -> None:
    """Update the immutable hook snapshot used by scheduled task runners."""
    global _ACTIVE_HOOK_REGISTRY
    _ACTIVE_HOOK_REGISTRY = hook_registry


# ---- Workflow scheduling primitives ----


class CronSchedule(BaseModel):
    """Standard cron-like schedule definition."""

    minute: str = Field(default="*", description="0-59, *, */5, or comma-separated")
    hour: str = Field(default="*", description="0-23, *, */2, or comma-separated")
    day: str = Field(default="*", description="1-31, *, or comma-separated")
    month: str = Field(default="*", description="1-12, *, or comma-separated")
    weekday: str = Field(default="*", description="0-6 (0=Sunday), *, or comma-separated")

    def to_cron_string(self) -> str:
        """Convert to standard cron format."""
        return f"{self.minute} {self.hour} {self.day} {self.month} {self.weekday}"

    def to_natural_language(self) -> str:
        """Convert cron schedule to natural language description."""
        try:
            cron_str = self.to_cron_string()
            options = Options(use_24hour_time_format=True)
            return str(get_description(cron_str, options))
        except Exception:
            return f"Cron: {self.to_cron_string()}"


class ScheduledWorkflow(BaseModel):
    """Structured representation of a scheduled task or workflow."""

    schedule_type: Literal["once", "cron"]
    is_conditional: bool = False
    execute_at: datetime | None = None
    cron_schedule: CronSchedule | None = None
    message: str
    description: str
    created_by: str | None = None
    thread_id: str | None = None
    room_id: str | None = None
    new_thread: bool = False


class _WorkflowParseError(BaseModel):
    """Error response when workflow parsing fails."""

    error: str
    suggestion: str | None = None


@dataclass
class ScheduledTaskRecord:
    """Parsed scheduled task state from Matrix."""

    task_id: str
    room_id: str
    status: str
    created_at: datetime | None
    workflow: ScheduledWorkflow


@dataclass(frozen=True)
class ScheduledTaskReadModel:
    """Display-neutral scheduled task fields derived from persisted state."""

    task_id: str
    room_id: str
    status: str
    schedule_type: Literal["once", "cron"]
    execute_at: datetime | None
    next_run_at: datetime | None
    cron_expression: str | None
    cron_description: str | None
    description: str
    message: str
    thread_id: str | None
    new_thread: bool
    created_by: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class SchedulingRuntime:
    """Live scheduling collaborators required to create or edit running tasks."""

    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    room: nio.MatrixRoom
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache
    matrix_admin: HookMatrixAdmin | None = None


@dataclass
class _DeferredOverdueTaskStart:
    """A one-time scheduled task that should start after Matrix sync is live."""

    task_id: str
    workflow: ScheduledWorkflow


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime string into a datetime object."""
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _raise_schedule_edit_error(message: str) -> typing.NoReturn:
    raise ValueError(message)


def build_scheduled_task_read_model(
    task: ScheduledTaskRecord,
    current_time: datetime | None = None,
) -> ScheduledTaskReadModel:
    """Build API/chat-neutral display fields for a scheduled task."""
    workflow = task.workflow
    cron_expression = workflow.cron_schedule.to_cron_string() if workflow.cron_schedule else None
    cron_description = workflow.cron_schedule.to_natural_language() if workflow.cron_schedule else None
    next_run_at = workflow.execute_at if workflow.schedule_type == "once" else None
    if workflow.schedule_type == "cron" and cron_expression:
        try:
            next_run_at = croniter(cron_expression, current_time or datetime.now(UTC)).get_next(datetime)
        except CroniterError:
            next_run_at = None

    return ScheduledTaskReadModel(
        task_id=task.task_id,
        room_id=task.room_id,
        status=task.status,
        schedule_type=workflow.schedule_type,
        execute_at=workflow.execute_at,
        next_run_at=next_run_at,
        cron_expression=cron_expression,
        cron_description=cron_description,
        description=workflow.description,
        message=workflow.message,
        thread_id=workflow.thread_id,
        new_thread=workflow.new_thread,
        created_by=workflow.created_by,
        created_at=task.created_at,
    )


def scheduled_task_read_sort_key(task: ScheduledTaskReadModel) -> tuple[int, datetime]:
    """Sort pending tasks first, then by next execution time."""
    status_rank = 0 if task.status == "pending" else 1
    scheduled_time = task.next_run_at or datetime.max.replace(tzinfo=UTC)
    return (status_rank, scheduled_time)


def build_edited_scheduled_workflow(  # noqa: C901
    existing_workflow: ScheduledWorkflow,
    room_id: str,
    *,
    message: str | None = None,
    description: str | None = None,
    schedule_type: Literal["once", "cron"] | None = None,
    execute_at: datetime | None = None,
    cron_expression: str | None = None,
) -> ScheduledWorkflow:
    """Build a validated patch-style workflow edit while preserving immutable metadata."""
    if schedule_type and schedule_type != existing_workflow.schedule_type:
        _raise_schedule_edit_error(_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR)

    schedule_type = existing_workflow.schedule_type
    if schedule_type == "once":
        if cron_expression is not None:
            _raise_schedule_edit_error("cron_expression is only valid for cron schedules")
        execute_at = execute_at or existing_workflow.execute_at
        if execute_at is None:
            _raise_schedule_edit_error("execute_at is required for one-time schedules")
        cron_schedule = None
    else:
        if execute_at is not None:
            _raise_schedule_edit_error("execute_at is only valid for one-time schedules")
        cron_schedule = existing_workflow.cron_schedule
        if cron_expression is not None:
            raw_expression = cron_expression.strip()
            fields = raw_expression.split()
            if len(fields) != 5:
                _raise_schedule_edit_error(
                    "Invalid cron expression: Cron expression must have exactly 5 fields: minute hour day month weekday",
                )
            try:
                croniter(raw_expression, datetime.now(UTC))
            except (ValueError, CroniterError) as e:
                _raise_schedule_edit_error(f"Invalid cron expression: {e!s}")
            minute, hour, day, month, weekday = fields
            cron_schedule = CronSchedule(minute=minute, hour=hour, day=day, month=month, weekday=weekday)
        if cron_schedule is None:
            _raise_schedule_edit_error("cron_expression is required for cron schedules")
        execute_at = None

    message_value = (message if message is not None else existing_workflow.message).strip()
    if not message_value:
        _raise_schedule_edit_error("message cannot be empty")
    description_value = (description if description is not None else existing_workflow.description).strip()

    return ScheduledWorkflow(
        schedule_type=schedule_type,
        execute_at=execute_at,
        cron_schedule=cron_schedule,
        message=message_value,
        description=description_value or message_value,
        created_by=existing_workflow.created_by,
        thread_id=existing_workflow.thread_id,
        room_id=room_id,
        new_thread=existing_workflow.new_thread,
    )


def _parse_scheduled_task_record(
    room_id: str,
    task_id: str,
    content: dict[str, object],
) -> ScheduledTaskRecord | None:
    """Parse a Matrix state event content payload into a scheduled task record."""
    status = str(content.get("status", "pending"))
    workflow_data_raw = content.get("workflow")
    if isinstance(workflow_data_raw, str):
        try:
            workflow = ScheduledWorkflow(**json.loads(workflow_data_raw))
        except (ValueError, json.JSONDecodeError):
            logger.exception("Failed to parse scheduled task workflow", room_id=room_id, task_id=task_id)
            return None
    else:
        return None

    created_at = _parse_datetime(content.get("created_at"))
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status=status,
        created_at=created_at,
        workflow=workflow,
    )


def _cancelled_task_content(
    task_id: str,
    existing_content: dict[str, object] | None,
) -> dict[str, object]:
    """Build cancelled task state while preserving existing metadata where possible."""
    cancelled_content: dict[str, object] = {"status": "cancelled", "task_id": task_id}

    if existing_content:
        workflow = existing_content.get("workflow")
        if isinstance(workflow, str):
            cancelled_content["workflow"] = workflow

        created_at = existing_content.get("created_at")
        if isinstance(created_at, str) and created_at:
            cancelled_content["created_at"] = created_at

        original_task_id = existing_content.get("task_id")
        if isinstance(original_task_id, str) and original_task_id:
            cancelled_content["task_id"] = original_task_id

    cancelled_content["updated_at"] = datetime.now(UTC).isoformat()
    return cancelled_content


def _is_polling_cron_schedule(cron_schedule: CronSchedule) -> bool:
    """Return whether a cron schedule looks like an interval-based polling cadence."""
    if cron_schedule.day != "*" or cron_schedule.month != "*" or cron_schedule.weekday != "*":
        return False

    minute = cron_schedule.minute.strip()
    hour = cron_schedule.hour.strip()

    def is_interval(field: str) -> bool:
        return field == "*" or field.startswith("*/")

    return (is_interval(minute) and is_interval(hour)) or (minute.isdigit() and is_interval(hour))


def _validate_conditional_workflow(
    workflow: ScheduledWorkflow,
) -> _WorkflowParseError | None:
    """Reject conditional parses that do not resolve to a polling-style recurring schedule."""
    if not workflow.is_conditional:
        return None

    if workflow.schedule_type != "cron" or workflow.cron_schedule is None:
        return _WorkflowParseError(
            error="Conditional schedules must resolve to a recurring polling schedule.",
            suggestion="Try again, or specify the polling cadence explicitly.",
        )

    cron_string = workflow.cron_schedule.to_cron_string()
    if _is_polling_cron_schedule(workflow.cron_schedule):
        return None

    return _WorkflowParseError(
        error=f"Conditional schedules must use a polling cron, but the parsed schedule was `{cron_string}`.",
        suggestion="Try again, or specify the polling cadence explicitly.",
    )


def _start_scheduled_task(
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> bool:
    """Start the asyncio task for a scheduled workflow and track it globally."""
    existing_task = _running_tasks.get(task_id)
    if existing_task is not None:
        if existing_task.done():
            del _running_tasks[task_id]
        else:
            logger.debug("Scheduled task already running; skipping duplicate start", task_id=task_id)
            return False

    if workflow.schedule_type == "once":
        task = asyncio.create_task(
            _run_once_task(
                client,
                task_id,
                workflow,
                config,
                runtime_paths,
                event_cache,
                conversation_cache,
                matrix_admin,
            ),
        )
    else:
        task = asyncio.create_task(
            _run_cron_task(
                client,
                task_id,
                workflow,
                _running_tasks,
                config,
                runtime_paths,
                conversation_cache,
                matrix_admin,
            ),
        )
    _running_tasks[task_id] = task
    return True


def _queue_deferred_overdue_task(task_id: str, workflow: ScheduledWorkflow) -> bool:
    """Queue one missed one-time task to be started after Matrix sync is ready."""
    existing_task = _running_tasks.get(task_id)
    if existing_task is not None and not existing_task.done():
        logger.debug("Scheduled task already running; skipping deferred queue", task_id=task_id)
        return False

    if task_id in _deferred_overdue_task_ids:
        logger.debug("Scheduled task already queued for deferred start", task_id=task_id)
        return False

    _deferred_overdue_tasks.append(_DeferredOverdueTaskStart(task_id=task_id, workflow=workflow))
    _deferred_overdue_task_ids.add(task_id)
    return True


async def drain_deferred_overdue_tasks(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
) -> int:
    """Start queued overdue one-time tasks after Matrix sync is ready."""
    drained_count = 0

    while _deferred_overdue_tasks:
        queued_task = _deferred_overdue_tasks.popleft()
        _deferred_overdue_task_ids.discard(queued_task.task_id)

        try:
            if _start_scheduled_task(
                client,
                queued_task.task_id,
                queued_task.workflow,
                config,
                runtime_paths,
                event_cache,
                conversation_cache,
                matrix_admin=build_hook_matrix_admin(client, runtime_paths),
            ):
                drained_count += 1
        except Exception:
            logger.exception(
                "Failed to start deferred overdue scheduled task",
                task_id=queued_task.task_id,
            )

        if _deferred_overdue_tasks:
            await asyncio.sleep(_DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)

    if drained_count > 0:
        logger.info("Drained deferred overdue scheduled tasks", drained_count=drained_count)

    return drained_count


def clear_deferred_overdue_tasks() -> int:
    """Clear queued overdue one-time tasks that have not started yet."""
    queued_count = len(_deferred_overdue_tasks)
    _deferred_overdue_tasks.clear()
    _deferred_overdue_task_ids.clear()
    return queued_count


def has_deferred_overdue_tasks() -> bool:
    """Return whether any overdue one-time tasks are still queued."""
    return bool(_deferred_overdue_tasks)


def _cancel_running_task(task_id: str) -> None:
    """Cancel a running scheduled task if it exists."""
    if task_id in _running_tasks:
        _running_tasks[task_id].cancel()
        del _running_tasks[task_id]


async def cancel_all_running_scheduled_tasks() -> int:
    """Cancel all in-memory scheduled tasks and wait for shutdown."""
    running_items = list(_running_tasks.items())
    if not running_items:
        return 0

    for task_id, task in running_items:
        task.cancel()
        del _running_tasks[task_id]

    await asyncio.gather(*(task for _, task in running_items), return_exceptions=True)

    return len(running_items)


def _workflows_differ(left: ScheduledWorkflow, right: ScheduledWorkflow) -> bool:
    """Return whether two workflows differ in persisted state."""
    return left.model_dump(mode="json") != right.model_dump(mode="json")


def _cleanup_task_if_current(task_id: str, running_tasks: dict[str, asyncio.Task]) -> None:
    """Remove task tracking if this coroutine still owns the task slot."""
    current_task = asyncio.current_task()
    if current_task and running_tasks.get(task_id) is current_task:
        del running_tasks[task_id]


def _parse_task_records_from_state(
    room_id: str,
    state_response: nio.RoomGetStateResponse,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Parse scheduled task records from a room state response."""
    tasks: list[ScheduledTaskRecord] = []
    for event in state_response.events:
        if event.get("type") != _SCHEDULED_TASK_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(state_key, str) or not isinstance(content, dict):
            continue

        task = _parse_scheduled_task_record(room_id, state_key, content)
        if not task:
            continue
        if not include_non_pending and task.status != "pending":
            continue
        tasks.append(task)

    return tasks


async def get_scheduled_tasks_for_room(
    client: nio.AsyncClient,
    room_id: str,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Fetch and parse scheduled tasks for a room."""
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        logger.error("Failed to get room state", response=str(response), room_id=room_id)
        return []

    return _parse_task_records_from_state(room_id, response, include_non_pending)


async def get_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> ScheduledTaskRecord | None:
    """Fetch and parse a single scheduled task from Matrix state."""
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )
    if not isinstance(response, nio.RoomGetStateEventResponse):
        return None
    if not isinstance(response.content, dict):
        return None
    return _parse_scheduled_task_record(room_id, task_id, response.content)


async def _get_pending_task_record(
    client: nio.AsyncClient,
    room_id: str | None,
    task_id: str,
) -> ScheduledTaskRecord | None:
    """Return the latest pending task state for a task id, if it still exists."""
    if not room_id:
        return None

    task_record = await get_scheduled_task(client=client, room_id=room_id, task_id=task_id)
    if not task_record or task_record.status != "pending":
        return None
    return task_record


def _serialize_scheduled_task_created_at(created_at: datetime | str | None) -> str:
    """Normalize persisted scheduled-task timestamps."""
    if isinstance(created_at, datetime):
        return created_at.isoformat()
    if isinstance(created_at, str) and created_at:
        return created_at
    return datetime.now(UTC).isoformat()


async def _persist_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    status: str = "pending",
    created_at: datetime | str | None = None,
) -> None:
    """Persist scheduled task state to Matrix."""
    await client.room_put_state(
        room_id=room_id,
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        content={
            "task_id": task_id,
            "workflow": workflow.model_dump_json(),
            "status": status,
            "created_at": _serialize_scheduled_task_created_at(created_at),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        state_key=task_id,
    )


async def _save_pending_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    created_at: datetime | str | None = None,
    matrix_admin: HookMatrixAdmin | None = None,
) -> None:
    """Persist one pending task and start or replace its in-memory runner."""
    _cancel_running_task(task_id)
    await _persist_scheduled_task_state(
        client=client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow,
        status="pending",
        created_at=created_at,
    )
    _start_scheduled_task(
        client,
        task_id,
        workflow,
        config,
        runtime_paths,
        event_cache,
        conversation_cache,
        matrix_admin,
    )


async def _save_one_time_task_status(
    client: nio.AsyncClient,
    task: ScheduledTaskRecord,
    status: str,
) -> None:
    """Persist the terminal status for a one-time task without restarting it."""
    await _persist_scheduled_task_state(
        client=client,
        room_id=task.room_id,
        task_id=task.task_id,
        workflow=task.workflow,
        status=status,
        created_at=task.created_at,
    )


async def save_edited_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    existing_task: ScheduledTaskRecord,
) -> ScheduledTaskRecord:
    """Persist edits to an existing task without touching runtime task runners."""
    if existing_task.status != "pending":
        msg = f"Task `{task_id}` cannot be edited because it is `{existing_task.status}`."
        raise ValueError(msg)

    if workflow.schedule_type != existing_task.workflow.schedule_type:
        raise ValueError(_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR)

    await _persist_scheduled_task_state(
        client=client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow,
        status="pending",
        created_at=existing_task.created_at,
    )

    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status="pending",
        created_at=existing_task.created_at,
        workflow=workflow,
    )


async def _parse_workflow_schedule(
    request: str,
    config: Config,
    runtime_paths: RuntimePaths,
    available_responders: typing.Sequence[MatrixID],
    current_time: datetime | None = None,
) -> ScheduledWorkflow | _WorkflowParseError:
    """Parse natural language into structured workflow using AI."""
    if current_time is None:
        current_time = datetime.now(UTC)

    if not available_responders:
        return _WorkflowParseError(
            error="No agents or teams available for scheduling.",
            suggestion="Try again in a room or thread where at least one agent or team can respond.",
        )
    registry = entity_identity_registry(config, runtime_paths)
    agent_list = ", ".join(
        f"@{entity_name}"
        for responder_id in available_responders
        if (entity_name := registry.current_entity_name_for_user_id(responder_id.full_id, include_router=False))
        is not None
    )

    prompt = config.render_prompt(
        "WORKFLOW_SCHEDULE_PARSE_PROMPT_TEMPLATE",
        current_time=current_time.isoformat(),
        request=request,
        agent_list=agent_list,
    )

    model = model_loading.get_model_instance(config, runtime_paths, "default")

    agent = Agent(
        name="WorkflowParser",
        role="Parse scheduling requests into structured workflows",
        model=model,
        output_schema=ScheduledWorkflow,
        telemetry=False,
    )

    try:
        response = await agent.arun(prompt, session_id=f"workflow_parse_{uuid.uuid4()}")
        result = response.content

        if isinstance(result, ScheduledWorkflow):
            if result.schedule_type == "once" and not result.execute_at:
                # Match previous behavior: default to 30 minutes from now
                result.execute_at = current_time + timedelta(minutes=30)
            elif result.schedule_type == "cron" and not result.cron_schedule:
                result.cron_schedule = CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")

            conditional_validation_error = _validate_conditional_workflow(result)
            if conditional_validation_error is not None:
                return conditional_validation_error

            logger.info("Successfully parsed workflow schedule", request=request, schedule_type=result.schedule_type)
            return result

        logger.error("Unexpected response type from AI", response_type=type(result).__name__)
        return _WorkflowParseError(
            error="Failed to parse the schedule request",
            suggestion="Try being more specific about the timing and what you want to happen",
        )

    except Exception as e:
        logger.exception("Error parsing workflow schedule", error=str(e), request=request)
        return _WorkflowParseError(
            error=f"Error parsing schedule: {e!s}",
            suggestion="Try a simpler format like 'Daily at 9am, check my email'",
        )


async def _build_workflow_message_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    message_text: str,
    conversation_cache: ConversationCacheProtocol,
) -> dict[str, typing.Any]:
    """Build Matrix message content for a scheduled workflow."""
    if workflow.new_thread:
        return format_message_with_mentions(
            config,
            runtime_paths,
            message_text,
            thread_event_id=None,
        )
    automated_message = (
        f"⏰ [Automated Task]\n{message_text}\n\n_Note: Automated task - follow-up expected when complete._"
    )
    assert workflow.room_id is not None  # Caller checks this
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            workflow.room_id,
            target.resolved_thread_id,
            caller_label="scheduled_workflow_message",
        )
    return format_message_with_mentions(
        config,
        runtime_paths,
        automated_message,
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def _build_scheduled_failure_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error_message: str,
    conversation_cache: ConversationCacheProtocol,
) -> dict[str, typing.Any]:
    """Build a failure message that follows the scheduled workflow target."""
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        assert workflow.room_id is not None
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            workflow.room_id,
            target.resolved_thread_id,
            caller_label="scheduled_workflow_failure",
        )
    return build_message_content(
        body=error_message,
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def _notify_scheduled_workflow_failure(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    config: Config,
    error: Exception,
    conversation_cache: ConversationCacheProtocol,
) -> None:
    """Send the visible failure notice for one scheduled workflow when possible."""
    if not workflow.room_id:
        return
    error_message = f"❌ Scheduled task failed: {workflow.description}\nError: {error!s}"
    error_content = await _build_scheduled_failure_content(
        workflow,
        target,
        error_message,
        conversation_cache,
    )
    try:
        await send_and_track_message(client, workflow.room_id, error_content, config, conversation_cache)
    except Exception:
        logger.exception("Failed to send scheduled workflow failure message")


async def _execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    task_id: str = "scheduled-task",
    matrix_admin: HookMatrixAdmin | None = None,
) -> bool:
    """Execute a scheduled workflow by posting its message to the thread."""
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return False

    target = MessageTarget.for_scheduled_task(
        workflow,
    )

    with bound_log_context(**target.log_context):
        try:
            message_text = workflow.message
            if _ACTIVE_HOOK_REGISTRY.has_hooks(EVENT_SCHEDULE_FIRED):
                context = ScheduleFiredContext(
                    event_name=EVENT_SCHEDULE_FIRED,
                    plugin_name="",
                    settings={},
                    config=config,
                    runtime_paths=runtime_paths,
                    logger=logger.bind(event_name=EVENT_SCHEDULE_FIRED),
                    correlation_id=f"{EVENT_SCHEDULE_FIRED}:{task_id}",
                    message_sender=build_hook_message_sender(
                        client,
                        config,
                        runtime_paths,
                        conversation_cache=conversation_cache,
                    ),
                    matrix_admin=matrix_admin,
                    room_state_querier=build_hook_room_state_querier(client),
                    room_state_putter=build_hook_room_state_putter(client),
                    task_id=task_id,
                    workflow=workflow,
                    room_id=workflow.room_id,
                    thread_id=target.resolved_thread_id,
                    created_by=workflow.created_by,
                    message_text=message_text,
                )
                await emit(_ACTIVE_HOOK_REGISTRY, EVENT_SCHEDULE_FIRED, context)
                if context.suppress:
                    logger.info("Scheduled workflow suppressed by hook", task_id=task_id, room_id=workflow.room_id)
                    return False
                message_text = context.message_text

            content = await _build_workflow_message_content(
                workflow,
                target,
                config,
                runtime_paths,
                message_text,
                conversation_cache,
            )
            if workflow.created_by:
                content[ORIGINAL_SENDER_KEY] = workflow.created_by
            content[SOURCE_KIND_KEY] = SCHEDULED_SOURCE_KIND
            delivered = await send_and_track_message(client, workflow.room_id, content, config, conversation_cache)
            if delivered is None:
                _raise_scheduled_workflow_send_error()
            logger.info(
                "Executed scheduled workflow",
                description=workflow.description,
                thread_id=target.resolved_thread_id,
                new_thread=workflow.new_thread,
                event_id=delivered.event_id,
            )
        except Exception as e:
            logger.exception("Failed to execute scheduled workflow")
            await _notify_scheduled_workflow_failure(
                client,
                workflow,
                target,
                config,
                e,
                conversation_cache,
            )
            return False
        else:
            return True


async def _run_cron_task(  # noqa: C901, PLR0911, PLR0912, PLR0915
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    running_tasks: dict[str, asyncio.Task],
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> None:
    """Run a recurring task based on cron schedule."""
    if not workflow.room_id:
        logger.error("No room_id provided for recurring task", task_id=task_id)
        return

    current_target = MessageTarget.for_scheduled_task(workflow)
    try:
        while True:
            latest_task = await _get_pending_task_record(client=client, room_id=workflow.room_id, task_id=task_id)
            if not latest_task:
                with bound_log_context(**current_target.log_context):
                    logger.info("Recurring task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow
            workflow = latest_workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
            with bound_log_context(**current_target.log_context):
                cron_schedule = latest_workflow.cron_schedule
                if not cron_schedule:
                    logger.error("No cron schedule provided for recurring task", task_id=task_id)
                    return

                cron_string = cron_schedule.to_cron_string()
                next_run = croniter(cron_string, datetime.now(UTC)).get_next(datetime)
                workflow_changed = False

                while True:
                    delay = (next_run - datetime.now(UTC)).total_seconds()
                    if delay <= 0:
                        break
                    await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))

                    refreshed_task = await _get_pending_task_record(
                        client=client,
                        room_id=workflow.room_id,
                        task_id=task_id,
                    )
                    if not refreshed_task:
                        logger.info("Recurring task cancelled while waiting, stopping", task_id=task_id)
                        return

                    refreshed_workflow = refreshed_task.workflow
                    if not refreshed_workflow.cron_schedule:
                        logger.error("No cron schedule provided for recurring task", task_id=task_id)
                        return

                    if _workflows_differ(workflow, refreshed_workflow):
                        workflow = refreshed_workflow
                        current_target = MessageTarget.for_scheduled_task(workflow)
                        workflow_changed = True
                        break

                if workflow_changed:
                    continue

                latest_before_execute = await _get_pending_task_record(
                    client=client,
                    room_id=workflow.room_id,
                    task_id=task_id,
                )
                if not latest_before_execute:
                    logger.info("Recurring task cancelled before execution, stopping", task_id=task_id)
                    return

                latest_workflow = latest_before_execute.workflow
                if not latest_workflow.cron_schedule:
                    logger.error("No cron schedule provided for recurring task", task_id=task_id)
                    return
                if _workflows_differ(workflow, latest_workflow):
                    workflow = latest_workflow
                    current_target = MessageTarget.for_scheduled_task(workflow)
                    continue

                await _execute_scheduled_workflow(
                    client,
                    workflow,
                    config,
                    runtime_paths,
                    conversation_cache,
                    task_id,
                    matrix_admin,
                )
                if task_id not in running_tasks:
                    logger.info("scheduled_task_missing_from_running_tasks", task_id=task_id)
                    return
    except asyncio.CancelledError:
        with bound_log_context(**current_target.log_context):
            logger.info("cron_task_cancelled", task_id=task_id)
        raise
    except Exception as e:
        with bound_log_context(**current_target.log_context):
            logger.exception("cron_task_failed", task_id=task_id)
            if workflow.room_id:
                error_message = f"❌ Recurring task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
                error_content = await _build_scheduled_failure_content(
                    workflow,
                    current_target,
                    error_message,
                    conversation_cache,
                )
                await send_and_track_message(client, workflow.room_id, error_content, config, conversation_cache)
    finally:
        _cleanup_task_if_current(task_id, running_tasks)


async def _run_once_task(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    _event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> None:
    """Run a one-time scheduled task."""
    if not workflow.room_id:
        logger.error("No room_id provided for one-time task", task_id=task_id)
        return

    current_target = MessageTarget.for_scheduled_task(workflow)
    latest_pending_task: ScheduledTaskRecord | None = None
    try:
        while True:
            latest_task = await _get_pending_task_record(client=client, room_id=workflow.room_id, task_id=task_id)
            if not latest_task:
                with bound_log_context(**current_target.log_context):
                    logger.info("One-time task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow
            workflow = latest_workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
            with bound_log_context(**current_target.log_context):
                execute_at = latest_workflow.execute_at
                if not execute_at:
                    logger.error("No execution time provided for one-time task", task_id=task_id)
                    return

                delay = (execute_at - datetime.now(UTC)).total_seconds()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))

        latest_before_execute = await _get_pending_task_record(
            client=client,
            room_id=workflow.room_id,
            task_id=task_id,
        )
        if not latest_before_execute:
            with bound_log_context(**current_target.log_context):
                logger.info("One-time task was cancelled before execution, stopping", task_id=task_id)
            return

        latest_workflow = latest_before_execute.workflow
        latest_pending_task = latest_before_execute
        workflow = latest_workflow
        current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            if not latest_workflow.execute_at:
                logger.error("No execution time provided for one-time task", task_id=task_id)
                return

            execution_succeeded = await _execute_scheduled_workflow(
                client,
                latest_workflow,
                config,
                runtime_paths,
                conversation_cache,
                task_id,
                matrix_admin,
            )
            final_status = "completed" if execution_succeeded else "failed"

            try:
                await _save_one_time_task_status(
                    client=client,
                    task=latest_pending_task,
                    status=final_status,
                )
            except Exception:
                logger.exception(
                    "Failed to persist one-time task final state",
                    task_id=task_id,
                    status=final_status,
                )
    except asyncio.CancelledError:
        if latest_pending_task is not None and latest_pending_task.workflow is not workflow:
            workflow = latest_pending_task.workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            logger.info("one_time_task_cancelled", task_id=task_id)
        raise
    except Exception as e:
        if latest_pending_task is not None and latest_pending_task.workflow is not workflow:
            workflow = latest_pending_task.workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            logger.exception("one_time_task_failed", task_id=task_id)
            if workflow.room_id:
                error_message = f"❌ One-time task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
                error_content = await _build_scheduled_failure_content(
                    workflow,
                    current_target,
                    error_message,
                    conversation_cache,
                )
                await send_and_track_message(client, workflow.room_id, error_content, config, conversation_cache)
            if latest_pending_task is not None:
                try:
                    await _save_one_time_task_status(
                        client=client,
                        task=latest_pending_task,
                        status="failed",
                    )
                except Exception:
                    logger.exception("Failed to mark one-time task as failed", task_id=task_id)
    finally:
        _cleanup_task_if_current(task_id, _running_tasks)


async def _validate_agent_mentions(
    message: str,
    allowed_agents: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> _AgentValidationResult:
    """Validate that all mentioned agents or teams are accessible.

    Args:
        message: The message that may contain @agent mentions
        allowed_agents: Agents the sender may target in this room
        config: Application configuration
        runtime_paths: Explicit runtime context for mention resolution

    Returns:
        _AgentValidationResult with validation status and agent lists

    """
    mentioned_agents = _extract_mentioned_agents_from_text(message, config, runtime_paths)
    if not mentioned_agents:
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    valid_agents: list[MatrixID] = []
    invalid_agents: list[MatrixID] = []

    for mid in mentioned_agents:
        if mid in allowed_agents:
            valid_agents.append(mid)
        else:
            invalid_agents.append(mid)

    return _AgentValidationResult(
        all_valid=len(invalid_agents) == 0,
        valid_agents=valid_agents,
        invalid_agents=invalid_agents,
    )


def _format_scheduled_time(dt: datetime, timezone_str: str) -> str:
    """Format a datetime with timezone and relative time delta.

    Args:
        dt: Datetime in UTC
        timezone_str: Timezone string (e.g., 'America/New_York')

    Returns:
        Formatted string like "2024-01-15 3:30 PM EST (in 2 hours)"

    """
    # Convert UTC to target timezone
    tz = ZoneInfo(timezone_str)
    local_dt = dt.astimezone(tz)

    # Get human-readable relative time using humanize
    now = datetime.now(UTC)
    relative_str = humanize.naturaltime(dt, when=now)

    # Format the datetime string with 24-hour time
    time_str = local_dt.strftime("%Y-%m-%d %H:%M %Z")
    return f"{time_str} ({relative_str})"


def _extract_mentioned_agents_from_text(
    full_text: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Extract valid agent mentions from scheduling text."""
    _, mentioned_user_ids, _ = parse_mentions_in_text(
        full_text,
        config,
        runtime_paths,
    )
    mentioned_agents: list[MatrixID] = []

    for user_id in mentioned_user_ids:
        matrix_id = MatrixID.parse(user_id)
        if (
            entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
                matrix_id.full_id,
                include_router=False,
            )
            and matrix_id not in mentioned_agents
        ):
            mentioned_agents.append(matrix_id)

    return mentioned_agents


async def schedule_task(  # noqa: C901, PLR0911, PLR0912, PLR0915
    runtime: SchedulingRuntime,
    room_id: str,
    thread_id: str | None,
    scheduled_by: str,
    full_text: str,
    new_thread: bool = False,
    mentioned_agents: list[MatrixID] | None = None,
    task_id: str | None = None,
    existing_task: ScheduledTaskRecord | None = None,
) -> tuple[str | None, str]:
    """Schedule a workflow from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    client = runtime.client
    config = runtime.config
    runtime_paths = runtime.runtime_paths
    room = runtime.room
    conversation_cache = runtime.conversation_cache
    event_cache = runtime.event_cache

    if mentioned_agents is None:
        mentioned_agents = _extract_mentioned_agents_from_text(full_text, config, runtime_paths)

    sender_visible_room_responders = await responder_candidate_entities_for_room(
        client,
        room,
        scheduled_by,
        config,
        runtime_paths,
    )

    available_responders: list[MatrixID] = []
    if new_thread:
        available_responders = list(sender_visible_room_responders)
    else:
        if thread_id:
            thread_history = list(
                await conversation_cache.get_thread_history(
                    room_id,
                    thread_id,
                    caller_label="schedule_existing_thread",
                ),
            )
            available_responders = filter_thread_agents_for_sender(
                get_agents_in_thread(thread_history, config, runtime_paths),
                scheduled_by,
                config,
                runtime_paths,
                available_responders_in_room=sender_visible_room_responders,
            )

        if mentioned_agents:
            for mid in mentioned_agents:
                if mid not in available_responders and mid in sender_visible_room_responders:
                    available_responders.append(mid)

        if not available_responders:
            available_responders = list(sender_visible_room_responders)

    if not available_responders:
        return (None, "❌ No agents or teams in this room are allowed to reply to you.")

    # Parse the workflow request with available responders.
    workflow_result = await _parse_workflow_schedule(full_text, config, runtime_paths, available_responders)

    if isinstance(workflow_result, _WorkflowParseError):
        error_msg = f"❌ {workflow_result.error}"
        if workflow_result.suggestion:
            error_msg += f"\n\n💡 {workflow_result.suggestion}"
        return (None, error_msg)

    # Handle workflow task
    # Validate workflow before proceeding
    if workflow_result.schedule_type == "once" and not workflow_result.execute_at:
        return (None, "❌ Failed to schedule: One-time task missing execution time")
    if workflow_result.schedule_type == "cron" and not workflow_result.cron_schedule:
        return (None, "❌ Failed to schedule: Recurring task missing cron schedule")

    # Validate that all mentioned agents or teams are accessible.
    validation_result = await _validate_agent_mentions(
        workflow_result.message,
        available_responders,
        config,
        runtime_paths,
    )

    if not validation_result.all_valid:
        scope = "room" if new_thread or not thread_id else "thread"
        error_msg = "❌ Failed to schedule: The following agents or teams are not available in this "
        error_msg += scope
        error_msg += f": {', '.join(agent.full_id for agent in validation_result.invalid_agents)}"

        # Provide helpful suggestions
        suggestions: list[str] = []
        registry = entity_identity_registry(config, runtime_paths)
        for agent in validation_result.invalid_agents:
            agent_name = registry.current_entity_name_for_user_id(agent.full_id, include_router=False)
            if agent_name:
                # Entity exists but is not available in this room/thread.
                suggestions.append(f"@{agent_name} is not available in this {scope}")
            else:
                suggestions.append(f"{agent.full_id} does not exist")

        if suggestions:
            error_msg += "\n\n💡 " + "\n💡 ".join(suggestions)

        return (None, error_msg)

    # Add metadata to workflow
    workflow_result.created_by = scheduled_by
    workflow_result.thread_id = None if new_thread else thread_id
    workflow_result.room_id = room_id
    workflow_result.new_thread = new_thread

    # Create task ID for new tasks (or reuse existing ID when editing)
    task_id = task_id or (existing_task.task_id if existing_task else str(uuid.uuid4())[:8])

    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=workflow_result.thread_id,
        new_thread=new_thread,
        schedule_type=workflow_result.schedule_type,
    )

    try:
        if existing_task:
            await save_edited_scheduled_task(
                client=client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                existing_task=existing_task,
            )
        else:
            await _save_pending_scheduled_task(
                client=client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                config=config,
                runtime_paths=runtime_paths,
                event_cache=event_cache,
                conversation_cache=conversation_cache,
                created_at=datetime.now(UTC).isoformat(),
                matrix_admin=runtime.matrix_admin,
            )
    except ValueError as e:
        return (None, f"❌ Failed to schedule: {e!s}")

    # Build success message
    if workflow_result.schedule_type == "once" and workflow_result.execute_at:
        # Format time with timezone and relative delta
        formatted_time = _format_scheduled_time(workflow_result.execute_at, config.timezone)
        success_msg = f"✅ Scheduled for {formatted_time}\n"
    elif workflow_result.cron_schedule:
        # Show both natural language and cron syntax
        natural_desc = workflow_result.cron_schedule.to_natural_language()
        cron_str = workflow_result.cron_schedule.to_cron_string()
        success_msg = f"✅ Scheduled recurring task: **{natural_desc}**\n"
        success_msg += f"   _(Cron: `{cron_str}`)_\n"
    else:
        success_msg = "✅ Task scheduled\n"

    success_msg += f"\n**Task:** {workflow_result.description}\n"
    success_msg += f"**Will post:** {workflow_result.message}\n"
    if new_thread:
        success_msg += "**Delivery:** New room-level thread root\n"
    success_msg += f"\n**Task ID:** `{task_id}`"

    return (task_id, success_msg)


async def edit_scheduled_task(
    runtime: SchedulingRuntime,
    room_id: str,
    task_id: str,
    full_text: str,
    scheduled_by: str,
    thread_id: str | None = None,
) -> str:
    """Edit an existing scheduled task by replacing its workflow details."""
    client = runtime.client
    existing_task = await get_scheduled_task(client=client, room_id=room_id, task_id=task_id)
    if not existing_task:
        return f"❌ Task `{task_id}` not found."
    if existing_task.status != "pending":
        return f"❌ Task `{task_id}` cannot be edited because it is `{existing_task.status}`."

    target_new_thread = existing_task.workflow.new_thread
    target_thread_id = None if target_new_thread else existing_task.workflow.thread_id or thread_id

    edited_task_id, response_text = await schedule_task(
        runtime=runtime,
        room_id=room_id,
        thread_id=target_thread_id,
        scheduled_by=scheduled_by,
        full_text=full_text,
        new_thread=target_new_thread,
        task_id=task_id,
        existing_task=existing_task,
    )

    if edited_task_id is None:
        return f"❌ Failed to edit task `{task_id}`.\n\n{response_text}"

    return f"✅ Updated task `{task_id}`.\n\n{response_text}"


async def list_scheduled_tasks(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None = None,
    config: Config | None = None,
) -> str:
    """List scheduled tasks in human-readable format."""
    # Pre-check: surface Matrix errors as user-facing messages
    state_response = await client.room_get_state(room_id)
    if not isinstance(state_response, nio.RoomGetStateResponse):
        logger.error("Failed to get room state", response=str(state_response), room_id=room_id, thread_id=thread_id)
        return "Unable to retrieve scheduled tasks."

    task_records = _parse_task_records_from_state(room_id, state_response, include_non_pending=False)

    tasks: list[ScheduledTaskRecord] = []
    tasks_in_other_threads: list[ScheduledTaskRecord] = []
    new_thread_tasks: list[ScheduledTaskRecord] = []

    for record in task_records:
        if thread_id:
            if record.workflow.new_thread:
                new_thread_tasks.append(record)
            elif record.workflow.thread_id and record.workflow.thread_id != thread_id:
                tasks_in_other_threads.append(record)
            else:
                tasks.append(record)
        else:
            tasks.append(record)

    if not tasks and not tasks_in_other_threads and not new_thread_tasks:
        return "No scheduled tasks found."

    if not tasks and tasks_in_other_threads and not new_thread_tasks:
        return f"No scheduled tasks in this thread.\n\n📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details."

    # Sort by execution time (one-time tasks) or put recurring tasks at the end
    def _sort_key(r: ScheduledTaskRecord) -> tuple[bool, datetime]:
        t = r.workflow.execute_at if r.workflow.schedule_type == "once" else None
        return (t is None, t or datetime.max.replace(tzinfo=UTC))

    tasks.sort(key=_sort_key)
    new_thread_tasks.sort(key=_sort_key)

    def _append_task_lines(lines: list[str], records: list[ScheduledTaskRecord]) -> None:
        for record in records:
            workflow = record.workflow
            if workflow.schedule_type == "once" and workflow.execute_at:
                timezone = config.timezone if config else "UTC"
                time_str = _format_scheduled_time(workflow.execute_at, timezone)
            else:
                time_str = workflow.cron_schedule.to_natural_language() if workflow.cron_schedule else "recurring"

            msg_preview = workflow.message[:_MESSAGE_PREVIEW_LENGTH] + (
                "..." if len(workflow.message) > _MESSAGE_PREVIEW_LENGTH else ""
            )
            lines.append(f'• `{record.task_id}` - {time_str}\n  {workflow.description}\n  Message: "{msg_preview}"')

    if tasks:
        lines = ["**Scheduled Tasks:**"]
        _append_task_lines(lines, tasks)
    else:
        lines = ["No scheduled tasks in this thread."]

    if new_thread_tasks:
        lines.append("")
        lines.append("**New Room-Level Thread Roots:**")
        _append_task_lines(lines, new_thread_tasks)

    if tasks_in_other_threads:
        lines.append("")
        lines.append(
            f"📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details.",
        )

    return "\n".join(lines)


async def cancel_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    cancel_in_memory: bool = True,
) -> str:
    """Cancel a scheduled task."""
    # Cancel the asyncio task if running
    if cancel_in_memory:
        _cancel_running_task(task_id)

    # First check if task exists
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )

    if not isinstance(response, nio.RoomGetStateEventResponse):
        return f"❌ Task `{task_id}` not found."

    # Update to cancelled
    existing_content = response.content if isinstance(response.content, dict) else None
    await client.room_put_state(
        room_id=room_id,
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        content=_cancelled_task_content(task_id, existing_content),
        state_key=task_id,
    )

    return f"✅ Cancelled task `{task_id}`"


async def cancel_all_scheduled_tasks(
    client: nio.AsyncClient,
    room_id: str,
) -> str:
    """Cancel all scheduled tasks in a room."""
    # Get all scheduled tasks
    response = await client.room_get_state(room_id)

    if not isinstance(response, nio.RoomGetStateResponse):
        logger.error("Failed to get room state", response=str(response))
        return "❌ Unable to retrieve scheduled tasks."

    cancelled_count = 0
    failed_count = 0

    for event in response.events:
        if event["type"] == _SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                task_id = event["state_key"]

                # Cancel the asyncio task if running
                _cancel_running_task(task_id)

                # Update to cancelled in Matrix state
                try:
                    existing_content = content if isinstance(content, dict) else None
                    await client.room_put_state(
                        room_id=room_id,
                        event_type=_SCHEDULED_TASK_EVENT_TYPE,
                        content=_cancelled_task_content(task_id, existing_content),
                        state_key=task_id,
                    )
                    cancelled_count += 1
                    logger.info("scheduled_task_cancelled", task_id=task_id)
                except Exception:
                    logger.exception("scheduled_task_cancel_failed", task_id=task_id)
                    failed_count += 1

    if cancelled_count == 0:
        return "No scheduled tasks to cancel."

    result = f"✅ Cancelled {cancelled_count} scheduled task(s)"
    if failed_count > 0:
        result += f"\n⚠️ Failed to cancel {failed_count} task(s)"

    return result


async def restore_scheduled_tasks(  # noqa: C901
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
) -> int:
    """Restore scheduled tasks from Matrix state after bot restart.

    Returns:
        Number of tasks restored

    """
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return 0

    restored_count = 0
    for task in _parse_task_records_from_state(room_id, response, include_non_pending=False):
        task_id = task.task_id
        workflow = task.workflow

        if workflow.schedule_type == "once":
            if not workflow.execute_at:
                logger.warning("skipping_one_time_task_without_execution_time", task_id=task_id)
                continue
            # Handle past one-time tasks: execute if within grace period, fail if too old
            now = datetime.now(UTC)
            if workflow.execute_at <= now:
                missed_by = (now - workflow.execute_at).total_seconds()
                if missed_by > _MISSED_TASK_MAX_AGE_SECONDS:
                    logger.warning(
                        "Skipping ancient missed task",
                        task_id=task_id,
                        missed_by_seconds=missed_by,
                    )
                    try:
                        await _persist_scheduled_task_state(
                            client=client,
                            room_id=room_id,
                            task_id=task_id,
                            workflow=workflow,
                            status="failed",
                            created_at=task.created_at,
                        )
                    except Exception:
                        logger.exception("Failed to mark ancient task as failed", task_id=task_id)
                    continue
                if _queue_deferred_overdue_task(task_id, workflow):
                    logger.warning(
                        "Queued missed one-time task until sync is ready",
                        task_id=task_id,
                        missed_by_seconds=missed_by,
                    )
                    restored_count += 1
                continue
        elif workflow.schedule_type == "cron" and not workflow.cron_schedule:
            logger.warning("skipping_recurring_task_without_cron_schedule", task_id=task_id)
            continue

        # Start the appropriate task
        if _start_scheduled_task(
            client,
            task_id,
            workflow,
            config,
            runtime_paths,
            event_cache,
            conversation_cache,
            matrix_admin=build_hook_matrix_admin(client, runtime_paths),
        ):
            restored_count += 1

    if restored_count > 0:
        logger.info("Restored scheduled tasks in room", room_id=room_id, restored_count=restored_count)

    return restored_count
