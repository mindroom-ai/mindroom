"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple
from zoneinfo import ZoneInfo

import humanize
import nio
from agno.agent import Agent
from cron_descriptor import get_description  # type: ignore[import-untyped]
from croniter import croniter  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger
from .matrix.client import (
    fetch_thread_history,
    get_latest_thread_event_id_if_needed,
    send_message,
)
from .matrix.identity import MatrixID
from .matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from .matrix.message_builder import build_message_content
from .thread_utils import get_agents_in_thread, get_available_agents_in_room

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

# Event type for scheduled tasks in Matrix state
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"

# Maximum length for message preview in task listings
MESSAGE_PREVIEW_LENGTH = 50

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


class WorkflowParseError(BaseModel):
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


def parse_scheduled_task_record(
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
) -> None:
    """Start the asyncio task for a scheduled workflow and track it globally."""
    if workflow.schedule_type == "once":
        task = asyncio.create_task(
            run_once_task(client, task_id, workflow, config),
        )
    else:
        task = asyncio.create_task(
            run_cron_task(client, task_id, workflow, _running_tasks, config),
        )
    _running_tasks[task_id] = task


def cancel_running_task(task_id: str) -> None:
    """Cancel a running scheduled task if it exists."""
    if task_id in _running_tasks:
        _running_tasks[task_id].cancel()
        del _running_tasks[task_id]


def _parse_task_records_from_state(
    room_id: str,
    state_response: nio.RoomGetStateResponse,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Parse scheduled task records from a room state response."""
    tasks: list[ScheduledTaskRecord] = []
    for event in state_response.events:
        if event.get("type") != SCHEDULED_TASK_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(state_key, str) or not isinstance(content, dict):
            continue

        task = parse_scheduled_task_record(room_id, state_key, content)
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
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )
    if not isinstance(response, nio.RoomGetStateEventResponse):
        return None
    if not isinstance(response.content, dict):
        return None
    return parse_scheduled_task_record(room_id, task_id, response.content)


async def save_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    status: str = "pending",
    created_at: datetime | str | None = None,
) -> None:
    """Persist scheduled task state and restart its in-memory task runner."""
    cancel_running_task(task_id)

    if isinstance(created_at, datetime):
        created_at_value = created_at.isoformat()
    elif isinstance(created_at, str) and created_at:
        created_at_value = created_at
    else:
        created_at_value = datetime.now(UTC).isoformat()

    await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        content={
            "task_id": task_id,
            "workflow": workflow.model_dump_json(),
            "status": status,
            "created_at": created_at_value,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        state_key=task_id,
    )

    if status == "pending":
        _start_scheduled_task(client, task_id, workflow, config)


async def parse_workflow_schedule(
    request: str,
    config: Config,
    available_agents: list[MatrixID],
    current_time: datetime | None = None,
) -> ScheduledWorkflow | WorkflowParseError:
    """Parse natural language into structured workflow using AI."""
    if current_time is None:
        current_time = datetime.now(UTC)

    assert available_agents, "No agents available for scheduling"
    agent_list = ", ".join(f"@{name}" for name in available_agents)

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

    model = get_model_instance(config, "default")

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

            logger.info("Successfully parsed workflow schedule", request=request, schedule_type=result.schedule_type)
            return result

        logger.error("Unexpected response type from AI", response_type=type(result).__name__)
        return WorkflowParseError(
            error="Failed to parse the schedule request",
            suggestion="Try being more specific about the timing and what you want to happen",
        )

    except Exception as e:
        logger.exception("Error parsing workflow schedule", error=str(e), request=request)
        return WorkflowParseError(
            error=f"Error parsing schedule: {e!s}",
            suggestion="Try a simpler format like 'Daily at 9am, check my email'",
        )


async def execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
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
            automated_message,
            sender_domain=config.domain,
            thread_event_id=workflow.thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )
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


async def run_cron_task(
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    running_tasks: dict[str, asyncio.Task],
    config: Config,
) -> None:
    """Run a recurring task based on cron schedule."""
    if not workflow.cron_schedule:
        logger.error("No cron schedule provided for recurring task")
        return

    cron_string = workflow.cron_schedule.to_cron_string()

    try:
        cron = croniter(cron_string, datetime.now(UTC))
        while True:
            next_run = cron.get_next(datetime)
            delay = (next_run - datetime.now(UTC)).total_seconds()
            if delay > 0:
                logger.info(
                    f"Waiting {delay:.0f} seconds until next execution",
                    task_id=task_id,
                    next_run=next_run.isoformat(),
                )
                await asyncio.sleep(delay)
            await execute_scheduled_workflow(client, workflow, config)
            if task_id not in running_tasks:
                logger.info(f"Task {task_id} no longer in running tasks, stopping")
                break
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


async def run_once_task(
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
) -> None:
    """Run a one-time scheduled task."""
    if not workflow.execute_at:
        logger.error("No execution time provided for one-time task")
        return

    try:
        delay = (workflow.execute_at - datetime.now(UTC)).total_seconds()
        if delay > 0:
            logger.info(
                f"Waiting {delay:.0f} seconds until execution",
                task_id=task_id,
                execute_at=workflow.execute_at.isoformat(),
            )
            await asyncio.sleep(delay)
        await execute_scheduled_workflow(client, workflow, config)
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


async def _validate_agent_mentions(
    message: str,
    room: nio.MatrixRoom,
    thread_id: str | None,
    config: Config,
) -> _AgentValidationResult:
    """Validate that all mentioned agents are accessible.

    Args:
        message: The message that may contain @agent mentions
        room: The Matrix room object
        thread_id: The thread ID where the schedule will execute (if in a thread)
        config: Application configuration

    Returns:
        _AgentValidationResult with validation status and agent lists

    """
    # Parse mentions - this handles all the agent name resolution properly
    _, mentioned_user_ids, _ = parse_mentions_in_text(message, config.domain, config)

    if not mentioned_user_ids:
        # No agents mentioned, validation passes
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    # Extract agent names from the mentioned user IDs

    mentioned_agents: list[MatrixID] = []
    for user_id in mentioned_user_ids:
        mid = MatrixID.parse(user_id)
        agent_name = mid.agent_name(config)
        if agent_name and mid not in mentioned_agents:
            mentioned_agents.append(mid)

    if not mentioned_agents:
        # No valid agents mentioned
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    valid_agents: list[MatrixID] = []
    invalid_agents: list[MatrixID] = []

    if thread_id:
        # For threads, check if agents are in the room
        room_agents = get_available_agents_in_room(room, config)

        # Agents can now respond in any room they're in
        for mid in mentioned_agents:
            if mid in room_agents:
                valid_agents.append(mid)
            else:
                invalid_agents.append(mid)
    else:
        # For room messages, check if agents are configured for the room
        room_agents = get_available_agents_in_room(room, config)
        for mid in mentioned_agents:
            if mid in room_agents:
                valid_agents.append(mid)
            else:
                invalid_agents.append(mid)

    all_valid = len(invalid_agents) == 0
    return _AgentValidationResult(
        all_valid=all_valid,
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


async def schedule_task(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    scheduled_by: str,
    full_text: str,
    config: Config,
    room: nio.MatrixRoom,
    mentioned_agents: list[MatrixID] | None = None,
) -> tuple[str | None, str]:
    """Schedule a workflow from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    # Get agents that are available in the thread
    available_agents: list[MatrixID] = []
    if thread_id:
        # Get agents already participating in the thread
        thread_history = await fetch_thread_history(client, room_id, thread_id)
        available_agents = get_agents_in_thread(thread_history, config)

    # Add any agents mentioned in the command itself
    if mentioned_agents:
        for mid in mentioned_agents:
            if mid not in available_agents:
                available_agents.append(mid)

    # If no agents found in thread or mentions, fall back to agents in the room
    if not available_agents:
        available_agents = get_available_agents_in_room(room, config)

    # Parse the workflow request with available agents
    workflow_result = await parse_workflow_schedule(full_text, config, available_agents)

    if isinstance(workflow_result, WorkflowParseError):
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
    validation_result = await _validate_agent_mentions(workflow_result.message, room, thread_id, config)

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
            agent_name = agent.agent_name(config)
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

    # Create task ID
    task_id = str(uuid.uuid4())[:8]

    created_at = datetime.now(UTC).isoformat()

    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=thread_id,
        schedule_type=workflow_result.schedule_type,
    )

    await save_scheduled_task(
        client=client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow_result,
        config=config,
        status="pending",
        created_at=created_at,
    )

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

        msg_preview = workflow.message[:MESSAGE_PREVIEW_LENGTH] + (
            "..." if len(workflow.message) > MESSAGE_PREVIEW_LENGTH else ""
        )
        lines.append(f'• `{record.task_id}` - {time_str}\n  {workflow.description}\n  Message: "{msg_preview}"')

    return "\n".join(lines)


async def cancel_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> str:
    """Cancel a scheduled task."""
    # Cancel the asyncio task if running
    cancel_running_task(task_id)

    # First check if task exists
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )

    if not isinstance(response, nio.RoomGetStateEventResponse):
        return f"❌ Task `{task_id}` not found."

    # Update to cancelled
    existing_content = response.content if isinstance(response.content, dict) else None
    await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
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
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                task_id = event["state_key"]

                # Cancel the asyncio task if running
                cancel_running_task(task_id)

                # Update to cancelled in Matrix state
                try:
                    existing_content = content if isinstance(content, dict) else None
                    await client.room_put_state(
                        room_id=room_id,
                        event_type=SCHEDULED_TASK_EVENT_TYPE,
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


async def restore_scheduled_tasks(client: nio.AsyncClient, room_id: str, config: Config) -> int:  # noqa: C901
    """Restore scheduled tasks from Matrix state after bot restart.

    Returns:
        Number of tasks restored

    """
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return 0

    restored_count = 0
    for event in response.events:
        if event["type"] != SCHEDULED_TASK_EVENT_TYPE:
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
            _start_scheduled_task(client, task_id, workflow, config)
            restored_count += 1

        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to restore task")
            continue

    if restored_count > 0:
        logger.info("Restored scheduled tasks in room", room_id=room_id, restored_count=restored_count)

    return restored_count
