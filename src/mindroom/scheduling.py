"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import nio

from .logging_config import get_logger
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


async def schedule_task(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    agent_user_id: str,  # noqa: ARG001
    scheduled_by: str,
    full_text: str,
    config: Config,
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
            run_once_task(client, task_id, workflow_result),
        )
    else:  # cron
        task = asyncio.create_task(
            run_cron_task(client, task_id, workflow_result, _running_tasks),
        )

    _running_tasks[task_id] = task

    # Build success message
    if workflow_result.schedule_type == "once" and workflow_result.execute_at:
        exec_time = workflow_result.execute_at.strftime("%Y-%m-%d %H:%M UTC")
        success_msg = f"✅ Scheduled for {exec_time}\n"
    elif workflow_result.cron_schedule:
        success_msg = f"✅ Scheduled recurring task: {workflow_result.cron_schedule.to_cron_string()}\n"
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
                        # For cron, show the cron pattern
                        display_time = None
                        schedule_type = (
                            workflow.cron_schedule.to_cron_string() if workflow.cron_schedule else "recurring"
                        )

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
            time_str = task["time"].strftime("%Y-%m-%d %H:%M UTC")
        else:
            time_str = f"Recurring: {task['schedule_type']}"

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


async def restore_scheduled_tasks(client: nio.AsyncClient, room_id: str) -> int:
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
            task_id = event["state_key"]

            # Parse the workflow
            workflow_data = json.loads(content["workflow"])
            workflow = ScheduledWorkflow(**workflow_data)

            # Only restore if still relevant
            if workflow.schedule_type == "once" and workflow.execute_at and workflow.execute_at <= datetime.now(UTC):
                continue  # Skip past one-time tasks

            # Start the appropriate task
            if workflow.schedule_type == "once":
                task = asyncio.create_task(run_once_task(client, task_id, workflow))
            else:
                task = asyncio.create_task(run_cron_task(client, task_id, workflow, _running_tasks))

            _running_tasks[task_id] = task
            restored_count += 1

        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to restore task")
            continue

    if restored_count > 0:
        logger.info(f"Restored {restored_count} scheduled tasks in room {room_id}")

    return restored_count
