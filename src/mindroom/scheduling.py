"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

import humanize
import nio
import pytz

from .logging_config import get_logger
from .matrix import MATRIX_HOMESERVER
from .matrix.identity import extract_agent_name, extract_server_name_from_homeserver
from .matrix.mentions import parse_mentions_in_text
from .thread_invites import ThreadInviteManager
from .thread_utils import get_available_agents_in_room
from .workflow_scheduling import (
    ScheduledWorkflow,
    WorkflowParseError,
    parse_workflow_schedule,
    run_cron_task,
    run_once_task,
)

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
    valid_agents: list[str]
    invalid_agents: list[str]


async def _validate_agent_mentions(
    message: str,
    room: nio.MatrixRoom,
    thread_id: str | None,
    config: Config,
    client: nio.AsyncClient,
) -> _AgentValidationResult:
    """Validate that all mentioned agents are accessible.

    Args:
        message: The message that may contain @agent mentions
        room: The Matrix room object
        thread_id: The thread ID where the schedule will execute (if in a thread)
        config: Application configuration
        client: Matrix client for checking thread invitations

    Returns:
        _AgentValidationResult with validation status and agent lists

    """
    # Use the existing parse_mentions_in_text to extract agent mentions
    # Get the proper server domain from the homeserver URL or MATRIX_SERVER_NAME env var
    server_domain = extract_server_name_from_homeserver(MATRIX_HOMESERVER)

    # Parse mentions - this handles all the agent name resolution properly
    _, mentioned_user_ids, _ = parse_mentions_in_text(message, server_domain, config)

    if not mentioned_user_ids:
        # No agents mentioned, validation passes
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    # Extract agent names from the mentioned user IDs

    mentioned_agents = []
    for user_id in mentioned_user_ids:
        agent_name = extract_agent_name(user_id, config)
        if agent_name and agent_name not in mentioned_agents:
            mentioned_agents.append(agent_name)

    if not mentioned_agents:
        # No valid agents mentioned
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    valid_agents = []
    invalid_agents = []

    if thread_id:
        # For threads, check both room agents and thread invitations
        thread_invite_manager = ThreadInviteManager(client)
        thread_agents = await thread_invite_manager.get_thread_agents(thread_id, room.room_id)

        # Also get agents naturally in the room
        room_agents = get_available_agents_in_room(room, config)

        # An agent is valid if it's either in the room or invited to the thread
        for agent_name in mentioned_agents:
            if agent_name in room_agents or agent_name in thread_agents:
                valid_agents.append(agent_name)
            else:
                invalid_agents.append(agent_name)
    else:
        # For room messages, check if agents are configured for the room
        room_agents = get_available_agents_in_room(room, config)

        for agent_name in mentioned_agents:
            if agent_name in room_agents:
                valid_agents.append(agent_name)
            else:
                invalid_agents.append(agent_name)

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
    tz = pytz.timezone(timezone_str)
    local_dt = dt.astimezone(tz)

    # Get human-readable relative time using humanize
    now = datetime.now(UTC)
    relative_str = humanize.naturaltime(dt, when=now)

    # Format the datetime string
    time_str = local_dt.strftime("%Y-%m-%d %I:%M %p %Z")
    return f"{time_str} ({relative_str})"


async def schedule_task(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    agent_user_id: str,  # noqa: ARG001
    scheduled_by: str,
    full_text: str,
    config: Config,
    room: nio.MatrixRoom,
) -> tuple[str | None, str]:
    """Schedule a workflow from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    # Parse the workflow request
    workflow_result = await parse_workflow_schedule(full_text, config)

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
    # Room must be provided by the caller
    if room is None:
        return (None, "❌ Internal error: Room object not provided")

    validation_result = await _validate_agent_mentions(
        workflow_result.message,
        room,
        thread_id,
        config,
        client,
    )

    if not validation_result.all_valid:
        error_msg = "❌ Failed to schedule: The following agents are not available in this "
        if thread_id:
            error_msg += "thread"
        else:
            error_msg += "room"
        error_msg += f": {', '.join(f'@{agent}' for agent in validation_result.invalid_agents)}"

        # Provide helpful suggestions
        suggestions = []
        for agent in validation_result.invalid_agents:
            if agent in config.agents:
                if thread_id:
                    suggestions.append(f"Use `!invite {agent}` to invite @{agent} to this thread")
                else:
                    # Agent exists but not configured for this room
                    suggestions.append(f"@{agent} is not configured for this room")
            else:
                suggestions.append(f"@{agent} does not exist")

        if suggestions:
            error_msg += "\n\n💡 " + "\n💡 ".join(suggestions)

        return (None, error_msg)

    # Add metadata to workflow
    workflow_result.created_by = scheduled_by
    workflow_result.thread_id = thread_id
    workflow_result.room_id = room_id

    # Create task ID
    task_id = str(uuid.uuid4())[:8]

    # Store workflow in Matrix state
    task_data = {
        "task_id": task_id,
        "workflow": workflow_result.model_dump_json(),
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
    }

    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=thread_id,
        schedule_type=workflow_result.schedule_type,
    )

    await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        content=task_data,
        state_key=task_id,
    )

    # Start the appropriate async task
    if workflow_result.schedule_type == "once":
        task = asyncio.create_task(
            run_once_task(client, task_id, workflow_result, config),
        )
    else:  # cron
        task = asyncio.create_task(
            run_cron_task(client, task_id, workflow_result, _running_tasks, config),
        )

    _running_tasks[task_id] = task

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


async def list_scheduled_tasks(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None = None,
    config: Config | None = None,
) -> str:
    """List scheduled tasks in human-readable format."""
    response = await client.room_get_state(room_id)

    if not isinstance(response, nio.RoomGetStateResponse):
        logger.error("Failed to get room state", response=str(response))
        return "Unable to retrieve scheduled tasks."

    tasks = []
    tasks_in_other_threads = []

    for event in response.events:
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                try:
                    # Parse the workflow
                    workflow_data = json.loads(content["workflow"])
                    workflow = ScheduledWorkflow(**workflow_data)

                    # Determine display time
                    if workflow.schedule_type == "once" and workflow.execute_at:
                        display_time = workflow.execute_at
                        schedule_type = "once"
                    else:
                        # For cron, show the natural language description
                        display_time = None
                        if workflow.cron_schedule:
                            schedule_type = workflow.cron_schedule.to_natural_language()
                        else:
                            schedule_type = "recurring"

                    task_info = {
                        "id": event["state_key"],
                        "time": display_time,
                        "schedule_type": schedule_type,
                        "description": workflow.description,
                        "message": workflow.message,
                        "thread_id": workflow.thread_id,
                    }

                    # Separate tasks by thread
                    if thread_id and workflow.thread_id and workflow.thread_id != thread_id:
                        tasks_in_other_threads.append(task_info)
                    else:
                        tasks.append(task_info)
                except (KeyError, ValueError, json.JSONDecodeError):
                    logger.exception("Failed to parse task")
                    continue

    if not tasks and not tasks_in_other_threads:
        return "No scheduled tasks found."

    if not tasks and tasks_in_other_threads:
        return f"No scheduled tasks in this thread.\n\n📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details."

    # Sort by execution time (one-time tasks) or put recurring tasks at the end
    tasks.sort(key=lambda t: (t["time"] is None, t["time"] or datetime.max.replace(tzinfo=UTC)))

    lines = ["**Scheduled Tasks:**"]
    for task in tasks:
        if task["schedule_type"] == "once" and task["time"]:
            # Get timezone from config or use UTC as fallback
            timezone = config.timezone if config else "UTC"
            time_str = _format_scheduled_time(task["time"], timezone)
        else:
            # For recurring tasks, schedule_type now contains the natural language description
            time_str = task["schedule_type"]

        msg_preview = task["message"][:MESSAGE_PREVIEW_LENGTH] + (
            "..." if len(task["message"]) > MESSAGE_PREVIEW_LENGTH else ""
        )
        lines.append(f'• `{task["id"]}` - {time_str}\n  {task["description"]}\n  Message: "{msg_preview}"')

    return "\n".join(lines)


async def cancel_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> str:
    """Cancel a scheduled task."""
    # Cancel the asyncio task if running
    if task_id in _running_tasks:
        _running_tasks[task_id].cancel()
        del _running_tasks[task_id]

    # First check if task exists
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )

    if not isinstance(response, nio.RoomGetStateEventResponse):
        return f"❌ Task `{task_id}` not found."

    # Update to cancelled
    await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        content={"status": "cancelled"},
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
                if task_id in _running_tasks:
                    _running_tasks[task_id].cancel()
                    del _running_tasks[task_id]

                # Update to cancelled in Matrix state
                try:
                    await client.room_put_state(
                        room_id=room_id,
                        event_type=SCHEDULED_TASK_EVENT_TYPE,
                        content={"status": "cancelled"},
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


async def restore_scheduled_tasks(client: nio.AsyncClient, room_id: str, config: Config) -> int:  # noqa: C901, PLR0912
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
            if workflow.schedule_type == "once":
                task = asyncio.create_task(run_once_task(client, task_id, workflow, config))
            else:
                task = asyncio.create_task(run_cron_task(client, task_id, workflow, _running_tasks, config))

            _running_tasks[task_id] = task
            restored_count += 1

        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to restore task")
            continue

    if restored_count > 0:
        logger.info(f"Restored {restored_count} scheduled tasks in room {room_id}")

    return restored_count
