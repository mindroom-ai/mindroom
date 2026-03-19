"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import re
import typing
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple
from zoneinfo import ZoneInfo

import humanize
import nio
from agno.agent import Agent
from cron_descriptor import get_description
from croniter import croniter
from pydantic import BaseModel, Field

from mindroom.ai import get_model_instance
from mindroom.authorization import get_available_agents_in_room
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.logging_config import get_logger
from mindroom.matrix.client import (
    fetch_thread_history,
    get_latest_thread_event_id_if_needed,
    send_message,
)
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from mindroom.matrix.message_builder import build_message_content
from mindroom.thread_utils import get_agents_in_thread

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Event type for scheduled tasks in Matrix state
_SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"

# Maximum length for message preview in task listings
_MESSAGE_PREVIEW_LENGTH = 50

# Shared validation message for edit attempts that change task type.
SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR = "Changing schedule_type is not supported; cancel and recreate the schedule"

# How often running tasks should re-check persisted Matrix state for edits/cancellations.
_TASK_STATE_POLL_INTERVAL_SECONDS = 30

# Global task storage for running asyncio tasks
_running_tasks: dict[str, asyncio.Task] = {}


class _AgentValidationResult(NamedTuple):
    """Result of agent mention validation."""

    all_valid: bool
    valid_agents: list[MatrixID]
    invalid_agents: list[MatrixID]


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
            return str(get_description(cron_str))
        except Exception:
            return f"Cron: {self.to_cron_string()}"


class ScheduledWorkflow(BaseModel):
    """Structured representation of a scheduled task or workflow."""

    schedule_type: Literal["once", "cron"]
    execute_at: datetime | None = None
    cron_schedule: CronSchedule | None = None
    message: str
    description: str
    created_by: str | None = None
    thread_id: str | None = None
    room_id: str | None = None


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


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime string into a datetime object."""
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


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
    elif status != "pending":
        # Backward compatibility: older cancellation paths wrote only {"status": "cancelled"}.
        description_value = content.get("description")
        description = (
            description_value if isinstance(description_value, str) and description_value else "Cancelled task"
        )
        message_value = content.get("message")
        message = message_value if isinstance(message_value, str) else ""
        thread_id_value = content.get("thread_id")
        thread_id = thread_id_value if isinstance(thread_id_value, str) else None
        created_by_value = content.get("created_by")
        created_by = created_by_value if isinstance(created_by_value, str) else None
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=None,
            message=message,
            description=description,
            created_by=created_by,
            thread_id=thread_id,
            room_id=room_id,
        )
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


def _start_scheduled_task(
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
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
            _run_once_task(client, task_id, workflow, config, runtime_paths),
        )
    else:
        task = asyncio.create_task(
            _run_cron_task(client, task_id, workflow, _running_tasks, config, runtime_paths),
        )
    _running_tasks[task_id] = task
    return True


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


async def _save_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    status: str = "pending",
    created_at: datetime | str | None = None,
    restart_task: bool = True,
) -> None:
    """Persist scheduled task state and optionally restart its in-memory task runner."""
    if restart_task:
        _cancel_running_task(task_id)

    if isinstance(created_at, datetime):
        created_at_value = created_at.isoformat()
    elif isinstance(created_at, str) and created_at:
        created_at_value = created_at
    else:
        created_at_value = datetime.now(UTC).isoformat()

    await client.room_put_state(
        room_id=room_id,
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        content={
            "task_id": task_id,
            "workflow": workflow.model_dump_json(),
            "status": status,
            "created_at": created_at_value,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        state_key=task_id,
    )

    if restart_task and status == "pending":
        _start_scheduled_task(client, task_id, workflow, config, runtime_paths)


async def save_edited_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    existing_task: ScheduledTaskRecord,
    restart_task: bool = False,
) -> ScheduledTaskRecord:
    """Persist edits to an existing task using shared validation semantics."""
    if existing_task.status != "pending":
        msg = f"Task `{task_id}` cannot be edited because it is `{existing_task.status}`."
        raise ValueError(msg)

    if workflow.schedule_type != existing_task.workflow.schedule_type:
        raise ValueError(SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR)

    await _save_scheduled_task(
        client=client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow,
        config=config,
        runtime_paths=runtime_paths,
        status="pending",
        created_at=existing_task.created_at,
        restart_task=restart_task,
    )

    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status="pending",
        created_at=existing_task.created_at,
        workflow=workflow,
    )


# Pattern matching simple interval requests like "every 5 minutes", "every 2 hours".
_INTERVAL_PATTERN = re.compile(
    r"\bevery\s+(\d+)\s+(minute|hour|min|hr)s?\b",
    re.IGNORECASE,
)

# Patterns that indicate conditional/event-driven scheduling.
_CONDITIONAL_PATTERNS = re.compile(
    r"\b(if\s+\w|when\s+\w|whenever\s|once\s+\w+\s+happens|on\s+condition)",
    re.IGNORECASE,
)


def _fix_interval_cron(request: str, cron: CronSchedule) -> CronSchedule:
    """Fix cron expressions for simple interval patterns the AI often gets wrong.

    When a user says "every N minutes" or "every N hours", the correct cron is
    ``*/N * * * *`` or ``0 */N * * *``.  Weak models frequently produce a fixed
    time (e.g. ``0 9 * * *``) instead.  This function detects the mismatch and
    returns a corrected CronSchedule.
    """
    match = _INTERVAL_PATTERN.search(request)
    if not match:
        return cron

    value = int(match.group(1))
    unit = match.group(2).lower()

    if unit in ("minute", "min"):
        expected_minute = f"*/{value}" if value > 1 else "*"
        if cron.minute != expected_minute or cron.hour != "*":
            logger.info(
                "Correcting cron for interval pattern",
                original=cron.to_cron_string(),
                corrected_minute=expected_minute,
                request=request,
            )
            return CronSchedule(minute=expected_minute, hour="*", day="*", month="*", weekday="*")
    elif unit in ("hour", "hr"):
        expected_hour = f"*/{value}" if value > 1 else "*"
        if cron.hour != expected_hour:
            logger.info(
                "Correcting cron for interval pattern",
                original=cron.to_cron_string(),
                corrected_hour=expected_hour,
                request=request,
            )
            return CronSchedule(minute="0", hour=expected_hour, day="*", month="*", weekday="*")

    return cron


def _validate_conditional_schedule(request: str, result: ScheduledWorkflow) -> _WorkflowParseError | None:
    """Return an error if a conditional request produced a schedule with no actionable message.

    When users write "if X then Y" or "when X do Y", the AI should embed the
    condition check into the message so the executing agent can evaluate it.
    If the message is empty or just whitespace, the condition was silently
    dropped — return a clear error instead.
    """
    if not _CONDITIONAL_PATTERNS.search(request):
        return None

    if result.message.strip():
        return None

    return _WorkflowParseError(
        error="Conditional schedule could not be created: the condition text was not preserved",
        suggestion=(
            "Conditional schedules (if/when/whenever) require the AI to embed the condition "
            "into the task message, but parsing failed.  Try rephrasing as a recurring check, "
            "e.g. '!schedule every 5 minutes check if <condition> and then <action>'"
        ),
    )


async def _parse_workflow_schedule(
    request: str,
    config: Config,
    runtime_paths: RuntimePaths,
    available_agents: typing.Sequence[MatrixID],
    current_time: datetime | None = None,
) -> ScheduledWorkflow | _WorkflowParseError:
    """Parse natural language into structured workflow using AI."""
    if current_time is None:
        current_time = datetime.now(UTC)

    assert available_agents, "No agents available for scheduling"
    agent_list = ", ".join(f"@{a.username}" for a in available_agents)

    prompt = f"""Parse this scheduling request into a structured workflow.

Current time (UTC): {current_time.isoformat()}Z
Request: "{request}"

Your task is to:
1. Determine if this is a one-time task or recurring (cron)
2. Extract the schedule/timing
3. Create a message that mentions the appropriate agents

Available agents: {agent_list}

IMPORTANT: Event-based and conditional requests:
When users say "if", "when", "whenever", "once X happens" or describe events/conditions:
1. Convert to an appropriate recurring (cron) schedule for polling
2. Include BOTH the condition check AND the action in the message
3. Choose polling frequency based on urgency and type

Important rules:
- For conditional/event-based requests, ALWAYS include the check condition in the message
- Mention relevant agents with @ only when needed
- Convert time expressions to UTC for the schedule, but DO NOT include them in the message
- Remove time phrases like "in 15 seconds" from the message itself
- If schedule_type is "once", you MUST provide execute_at
- If schedule_type is "cron", you MUST provide cron_schedule

Examples of event/condition phrasing to include in the message (do not include times in these examples):
- @email_assistant Check for emails containing 'urgent'. If found, @phone_agent notify the user.
- @crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user.
- @monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers.
- @reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points.
"""

    model = get_model_instance(config, runtime_paths, "default")

    agent = Agent(
        name="WorkflowParser",
        role="Parse scheduling requests into structured workflows",
        model=model,
        output_schema=ScheduledWorkflow,
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

            # Fix 1: Correct obviously wrong cron for simple interval patterns
            if result.schedule_type == "cron" and result.cron_schedule:
                result.cron_schedule = _fix_interval_cron(request, result.cron_schedule)

            # Fix 2: Reject conditional schedules where the condition was silently lost
            conditional_error = _validate_conditional_schedule(request, result)
            if conditional_error is not None:
                return conditional_error

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


async def _execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Execute a scheduled workflow by posting its message to the thread."""
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return

    try:
        automated_message = (
            f"⏰ [Automated Task]\n{workflow.message}\n\n_Note: Automated task - no follow-up expected._"
        )
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            client,
            workflow.room_id,
            workflow.thread_id,
        )
        content = format_message_with_mentions(
            config,
            runtime_paths,
            automated_message,
            sender_domain=config.get_domain(runtime_paths),
            thread_event_id=workflow.thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )
        if workflow.created_by:
            content[ORIGINAL_SENDER_KEY] = workflow.created_by
        await send_message(client, workflow.room_id, content)
        logger.info("Executed scheduled workflow", description=workflow.description, thread_id=workflow.thread_id)
    except Exception as e:
        logger.exception("Failed to execute scheduled workflow")
        if workflow.room_id:
            error_message = f"❌ Scheduled task failed: {workflow.description}\nError: {e!s}"
            error_content = build_message_content(
                body=error_message,
                thread_event_id=workflow.thread_id,
                latest_thread_event_id=workflow.thread_id,
            )
            await send_message(client, workflow.room_id, error_content)


async def _run_cron_task(  # noqa: C901, PLR0911, PLR0912, PLR0915
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    running_tasks: dict[str, asyncio.Task],
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Run a recurring task based on cron schedule."""
    if not workflow.room_id:
        logger.error("No room_id provided for recurring task", task_id=task_id)
        return

    try:
        while True:
            latest_task = await _get_pending_task_record(client=client, room_id=workflow.room_id, task_id=task_id)
            if not latest_task:
                logger.info("Recurring task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow

            cron_schedule = latest_workflow.cron_schedule
            if not cron_schedule:
                logger.error("No cron schedule provided for recurring task", task_id=task_id)
                return

            workflow = latest_workflow
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
                continue

            await _execute_scheduled_workflow(client, workflow, config, runtime_paths)
            if task_id not in running_tasks:
                logger.info(f"Task {task_id} no longer in running tasks, stopping")
                return
    except asyncio.CancelledError:
        logger.info(f"Cron task {task_id} was cancelled")
        raise
    except Exception as e:
        logger.exception(f"Error in cron task {task_id}")
        if workflow.room_id:
            error_message = f"❌ Recurring task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
            error_content = build_message_content(
                body=error_message,
                thread_event_id=workflow.thread_id,
                latest_thread_event_id=workflow.thread_id,
            )
            await send_message(client, workflow.room_id, error_content)
    finally:
        _cleanup_task_if_current(task_id, running_tasks)


async def _run_once_task(  # noqa: C901
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Run a one-time scheduled task."""
    if not workflow.room_id:
        logger.error("No room_id provided for one-time task", task_id=task_id)
        return

    try:
        while True:
            latest_task = await _get_pending_task_record(client=client, room_id=workflow.room_id, task_id=task_id)
            if not latest_task:
                logger.info("One-time task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow

            execute_at = latest_workflow.execute_at
            if not execute_at:
                logger.error("No execution time provided for one-time task", task_id=task_id)
                return

            workflow = latest_workflow
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
            logger.info("One-time task was cancelled before execution, stopping", task_id=task_id)
            return

        latest_workflow = latest_before_execute.workflow
        if not latest_workflow.execute_at:
            logger.error("No execution time provided for one-time task", task_id=task_id)
            return

        await _execute_scheduled_workflow(client, latest_workflow, config, runtime_paths)
    except asyncio.CancelledError:
        logger.info(f"One-time task {task_id} was cancelled")
        raise
    except Exception as e:
        logger.exception(f"Error in one-time task {task_id}")
        if workflow.room_id:
            error_message = f"❌ One-time task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
            error_content = build_message_content(
                body=error_message,
                thread_event_id=workflow.thread_id,
                latest_thread_event_id=workflow.thread_id,
            )
            await send_message(client, workflow.room_id, error_content)
    finally:
        _cleanup_task_if_current(task_id, _running_tasks)


async def _validate_agent_mentions(
    message: str,
    room: nio.MatrixRoom,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _AgentValidationResult:
    """Validate that all mentioned agents are accessible.

    Args:
        message: The message that may contain @agent mentions
        room: The Matrix room object
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

    room_agents = get_available_agents_in_room(room, config, runtime_paths)
    for mid in mentioned_agents:
        if mid in room_agents:
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
        config.get_domain(runtime_paths),
        config,
        runtime_paths,
    )
    mentioned_agents: list[MatrixID] = []

    for user_id in mentioned_user_ids:
        matrix_id = MatrixID.parse(user_id)
        if matrix_id.agent_name(config, runtime_paths) and matrix_id not in mentioned_agents:
            mentioned_agents.append(matrix_id)

    return mentioned_agents


async def schedule_task(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    scheduled_by: str,
    full_text: str,
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom,
    mentioned_agents: list[MatrixID] | None = None,
    task_id: str | None = None,
    existing_task: ScheduledTaskRecord | None = None,
    restart_task: bool = True,
) -> tuple[str | None, str]:
    """Schedule a workflow from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    if mentioned_agents is None:
        mentioned_agents = _extract_mentioned_agents_from_text(full_text, config, runtime_paths)

    # Get agents that are available in the thread
    available_agents: list[MatrixID] = []
    if thread_id:
        # Get agents already participating in the thread
        thread_history = await fetch_thread_history(client, room_id, thread_id)
        available_agents = get_agents_in_thread(thread_history, config, runtime_paths)

    # Add any agents mentioned in the command itself
    if mentioned_agents:
        for mid in mentioned_agents:
            if mid not in available_agents:
                available_agents.append(mid)

    # If no agents found in thread or mentions, fall back to agents in the room
    if not available_agents:
        available_agents = get_available_agents_in_room(room, config, runtime_paths)

    # Parse the workflow request with available agents
    workflow_result = await _parse_workflow_schedule(full_text, config, runtime_paths, available_agents)

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

    # Validate that all mentioned agents are accessible
    validation_result = await _validate_agent_mentions(workflow_result.message, room, config, runtime_paths)

    if not validation_result.all_valid:
        error_msg = "❌ Failed to schedule: The following agents are not available in this "
        if thread_id:
            error_msg += "thread"
        else:
            error_msg += "room"
        error_msg += f": {', '.join(agent.full_id for agent in validation_result.invalid_agents)}"

        # Provide helpful suggestions
        suggestions: list[str] = []
        for agent in validation_result.invalid_agents:
            agent_name = agent.agent_name(config, runtime_paths)
            if agent_name:
                # Agent exists but not available in this room/thread
                suggestions.append(f"{agent.full_id} is not available in this {'thread' if thread_id else 'room'}")
            else:
                suggestions.append(f"{agent.full_id} does not exist")

        if suggestions:
            error_msg += "\n\n💡 " + "\n💡 ".join(suggestions)

        return (None, error_msg)

    # Add metadata to workflow
    workflow_result.created_by = scheduled_by
    workflow_result.thread_id = thread_id
    workflow_result.room_id = room_id

    # Create task ID for new tasks (or reuse existing ID when editing)
    task_id = task_id or (existing_task.task_id if existing_task else str(uuid.uuid4())[:8])

    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=thread_id,
        schedule_type=workflow_result.schedule_type,
    )

    try:
        if existing_task:
            await save_edited_scheduled_task(
                client=client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                config=config,
                runtime_paths=runtime_paths,
                existing_task=existing_task,
                restart_task=restart_task,
            )
        else:
            await _save_scheduled_task(
                client=client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                config=config,
                runtime_paths=runtime_paths,
                status="pending",
                created_at=datetime.now(UTC).isoformat(),
                restart_task=restart_task,
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
    success_msg += f"\n**Task ID:** `{task_id}`"

    return (task_id, success_msg)


async def edit_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    full_text: str,
    scheduled_by: str,
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom,
    thread_id: str | None = None,
) -> str:
    """Edit an existing scheduled task by replacing its workflow details."""
    existing_task = await get_scheduled_task(client=client, room_id=room_id, task_id=task_id)
    if not existing_task:
        return f"❌ Task `{task_id}` not found."
    if existing_task.status != "pending":
        return f"❌ Task `{task_id}` cannot be edited because it is `{existing_task.status}`."

    # Keep the task in its original thread when possible.
    target_thread_id = existing_task.workflow.thread_id or thread_id

    edited_task_id, response_text = await schedule_task(
        client=client,
        room_id=room_id,
        thread_id=target_thread_id,
        scheduled_by=scheduled_by,
        full_text=full_text,
        config=config,
        runtime_paths=runtime_paths,
        room=room,
        task_id=task_id,
        existing_task=existing_task,
        restart_task=False,
    )

    if edited_task_id is None:
        return f"❌ Failed to edit task `{task_id}`.\n\n{response_text}"

    return f"✅ Updated task `{task_id}`.\n\n{response_text}"


async def list_scheduled_tasks(
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

    for record in task_records:
        if thread_id and record.workflow.thread_id and record.workflow.thread_id != thread_id:
            tasks_in_other_threads.append(record)
        else:
            tasks.append(record)

    if not tasks and not tasks_in_other_threads:
        return "No scheduled tasks found."

    if not tasks and tasks_in_other_threads:
        return f"No scheduled tasks in this thread.\n\n📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details."

    # Sort by execution time (one-time tasks) or put recurring tasks at the end
    def _sort_key(r: ScheduledTaskRecord) -> tuple[bool, datetime]:
        t = r.workflow.execute_at if r.workflow.schedule_type == "once" else None
        return (t is None, t or datetime.max.replace(tzinfo=UTC))

    tasks.sort(key=_sort_key)

    lines = ["**Scheduled Tasks:**"]
    for record in tasks:
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
                    logger.info(f"Cancelled task {task_id}")
                except Exception:
                    logger.exception(f"Failed to cancel task {task_id}")
                    failed_count += 1

    if cancelled_count == 0:
        return "No scheduled tasks to cancel."

    result = f"✅ Cancelled {cancelled_count} scheduled task(s)"
    if failed_count > 0:
        result += f"\n⚠️ Failed to cancel {failed_count} task(s)"

    return result


async def restore_scheduled_tasks(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> int:
    """Restore scheduled tasks from Matrix state after bot restart.

    Returns:
        Number of tasks restored

    """
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return 0

    restored_count = 0
    for event in response.events:
        if event["type"] != _SCHEDULED_TASK_EVENT_TYPE:
            continue

        content = event["content"]
        if content.get("status") != "pending":
            continue

        try:
            task_id: str = event["state_key"]

            # Parse the workflow
            workflow_data = json.loads(content["workflow"])
            workflow = ScheduledWorkflow(**workflow_data)

            # Validate workflow has required fields
            if workflow.schedule_type == "once":
                if not workflow.execute_at:
                    logger.warning(f"Skipping one-time task {task_id} without execution time")
                    continue
                # Skip past one-time tasks
                if workflow.execute_at <= datetime.now(UTC):
                    logger.debug(f"Skipping past one-time task {task_id}")
                    continue
            elif workflow.schedule_type == "cron":
                if not workflow.cron_schedule:
                    logger.warning(f"Skipping recurring task {task_id} without cron schedule")
                    continue
            else:
                logger.warning(f"Unknown schedule type for task {task_id}: {workflow.schedule_type}")
                continue

            # Start the appropriate task
            if _start_scheduled_task(client, task_id, workflow, config, runtime_paths):
                restored_count += 1

        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to restore task")
            continue

    if restored_count > 0:
        logger.info("Restored scheduled tasks in room", room_id=room_id, restored_count=restored_count)

    return restored_count
