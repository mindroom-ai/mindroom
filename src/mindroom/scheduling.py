"""Scheduled task management with AI-powered natural language time parsing."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import nio
from agno.agent import Agent
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger
from .matrix.client import send_message

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

# Event type for scheduled tasks in Matrix state
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"

# Global task storage for running asyncio tasks
_running_tasks: dict[str, asyncio.Task] = {}


class ScheduledTimeResponse(BaseModel):
    """Structured output for schedule parsing results."""

    execute_at: datetime = Field(description="The exact datetime when the task should execute (in UTC)")
    message: str = Field(description="The message/reminder text to send at the scheduled time")
    interpretation: str = Field(description="Human-readable explanation of the parsed time")


class ScheduleParseError(BaseModel):
    """Structured output when time parsing fails."""

    error: str = Field(description="Explanation of why the time couldn't be parsed")
    suggestion: str | None = Field(default=None, description="Suggestion for how to rephrase the time")


async def parse_schedule(
    full_text: str,
    config: Config,
    current_time: datetime | None = None,
) -> ScheduledTimeResponse | ScheduleParseError:
    """Use AI with structured output to parse natural language schedule requests."""
    if current_time is None:
        current_time = datetime.now(UTC)

    prompt = f"""Parse the following schedule request into a specific datetime and message.

Current time (UTC): {current_time.isoformat()}Z
Request: "{full_text}"

You MUST extract:
1. execute_at: The exact UTC datetime when the task should execute
2. message: The message/reminder text to send at that time
3. interpretation: A human-readable explanation of the parsed time

Examples of requests and how to parse them:
- "in 5 minutes Check the deployment status" -> execute_at: current_time + 5 minutes, message: "Check the deployment status"
- "tomorrow at 3pm Send the weekly report" -> execute_at: tomorrow at 15:00 UTC, message: "Send the weekly report"
- "later Ping me about the meeting" -> execute_at: current_time + 30 minutes, message: "Ping me about the meeting"
- "in 2 hours" -> execute_at: current_time + 2 hours, message: "Reminder"
- "remind me tomorrow" -> execute_at: tomorrow at 09:00 UTC, message: "Reminder"

Rules:
- For vague times like "later" or "soon", use 30 minutes from now
- If no message is specified, use "Reminder" as the default
- For "daily" requests, schedule tomorrow at 9am UTC (we don't support recurring yet)
- Parse time expressions flexibly: "1 min", "1 minute", "one minute" are all valid

IMPORTANT: Always provide a valid response. If the request is unclear, make a reasonable interpretation."""

    # Use default model for simplicity
    model = get_model_instance(config, "default")

    agent = Agent(
        name="ScheduleParser",
        role="Parse natural language schedule requests",
        model=model,
        response_model=ScheduledTimeResponse,  # Only use single model, not union
    )

    try:
        response = await agent.arun(prompt, session_id=f"schedule_parse_{uuid.uuid4()}")
        result = response.content

        if isinstance(result, ScheduledTimeResponse):
            logger.info(
                "Successfully parsed schedule",
                request=full_text,
                execute_at=result.execute_at,
                message=result.message,
                interpretation=result.interpretation,
            )
            return result
        if isinstance(result, ScheduleParseError):
            logger.debug("AI returned parse error", error=result.error)
            return result

        # Log unexpected response type for debugging
        logger.error(
            "Unexpected response type from AI",
            response_type=type(result).__name__,
            response_content=str(result),
        )
    except Exception as e:
        logger.exception("Error parsing schedule", error=str(e), request=full_text)

    # Fallback if AI returns unexpected type or errors
    return ScheduleParseError(
        error="Unable to parse the schedule request",
        suggestion="Try something like 'in 5 minutes Check the deployment' or 'tomorrow at 3pm Send report'",
    )


async def schedule_task(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    agent_user_id: str,
    scheduled_by: str,
    full_text: str,
    config: Config,
) -> tuple[str | None, str]:
    """Schedule a task from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    # Parse the full request
    parse_result = await parse_schedule(full_text, config)

    if isinstance(parse_result, ScheduleParseError):
        error_msg = f"❌ {parse_result.error}"
        if parse_result.suggestion:
            error_msg += f"\n\n💡 {parse_result.suggestion}"
        return (None, error_msg)

    # Create task ID
    task_id = str(uuid.uuid4())[:8]  # Short ID for user convenience

    # Store task in Matrix state
    task_data = {
        "task_id": task_id,
        "room_id": room_id,
        "thread_id": thread_id,
        "agent_user_id": agent_user_id,
        "scheduled_by": scheduled_by,
        "scheduled_at": datetime.now(UTC).isoformat(),
        "execute_at": parse_result.execute_at.isoformat(),
        "message": parse_result.message,
        "status": "pending",
    }

    # DEBUG: Log what we're storing
    logger.info(
        "Storing scheduled task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=thread_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key=task_id,
    )

    put_response = await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        content=task_data,
        state_key=task_id,
    )

    # DEBUG: Log the response
    logger.info(
        "room_put_state response",
        response_type=type(put_response).__name__,
        is_success=isinstance(put_response, nio.RoomPutStateResponse),
        response=str(put_response),
    )

    # Start the async task
    task = asyncio.create_task(
        _execute_scheduled_task(
            client,
            task_id,
            room_id,
            thread_id,
            agent_user_id,
            parse_result.execute_at,
            parse_result.message,
        ),
    )
    _running_tasks[task_id] = task

    # Build success message
    success_msg = f"✅ {parse_result.interpretation}\n\n"
    success_msg += f'I\'ll send: "{parse_result.message}"\n\n'
    success_msg += f"Task ID: `{task_id}`"

    return (task_id, success_msg)


async def _execute_scheduled_task(
    client: nio.AsyncClient,
    task_id: str,
    room_id: str,
    thread_id: str | None,
    agent_user_id: str,
    execute_at: datetime,
    message: str,
) -> None:
    """Execute a scheduled task at the specified time."""
    try:
        # Calculate delay
        delay = (execute_at - datetime.now(UTC)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        # Send the scheduled message
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"⏰ Scheduled reminder: {message}",
        }

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        await send_message(client, room_id, content)

        # Update task status to completed
        await client.room_put_state(
            room_id=room_id,
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            content={"status": "completed"},
            state_key=task_id,
        )

        # Clean up
        _running_tasks.pop(task_id, None)

    except asyncio.CancelledError:
        logger.info(f"Scheduled task {task_id} was cancelled")
        raise
    except Exception:
        logger.exception("Failed to execute scheduled task %s", task_id)


async def list_scheduled_tasks(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None = None,
) -> str:
    """List scheduled tasks in human-readable format."""
    # DEBUG: Log the request
    logger.info(
        "list_scheduled_tasks called",
        room_id=room_id,
        thread_id=thread_id,
    )

    response = await client.room_get_state(room_id)

    # DEBUG: Log the response type
    logger.info(
        "room_get_state response",
        response_type=type(response).__name__,
        is_success=isinstance(response, nio.RoomGetStateResponse),
    )

    if not isinstance(response, nio.RoomGetStateResponse):
        logger.error(
            "Failed to get room state",
            response=str(response),
            response_type=type(response).__name__,
        )
        return "Unable to retrieve scheduled tasks."

    tasks = []
    tasks_in_other_threads = []

    # DEBUG: Log event types
    event_types: dict[str, int] = {}
    for event in response.events:
        event_type = event.get("type", "unknown")
        event_types[event_type] = event_types.get(event_type, 0) + 1

    logger.info(
        "Room state events",
        total_events=len(response.events),
        event_types=event_types,
        scheduled_task_events=event_types.get(SCHEDULED_TASK_EVENT_TYPE, 0),
    )

    for event in response.events:
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                try:
                    # Get the thread_id from the task
                    task_thread = content.get("thread_id")

                    task_info = {
                        "id": event["state_key"],
                        "time": datetime.fromisoformat(content["execute_at"]),
                        "message": content["message"],
                        "thread_id": task_thread,
                    }

                    # Separate tasks by thread
                    # Only filter by thread if we're in a thread context
                    # Show all tasks if no thread_id (shouldn't happen with current design)
                    if thread_id and task_thread and task_thread != thread_id:
                        tasks_in_other_threads.append(task_info)
                    else:
                        tasks.append(task_info)
                except (KeyError, ValueError):
                    continue

    if not tasks and not tasks_in_other_threads:
        return "No scheduled tasks found."

    if not tasks and tasks_in_other_threads:
        return f"No scheduled tasks in this thread.\n\n📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details."

    # Sort by execution time
    tasks.sort(key=lambda t: t["time"])

    lines = ["**Scheduled Tasks:**"]
    for task in tasks:
        time_str = task["time"].strftime("%Y-%m-%d %H:%M UTC")
        msg_preview = task["message"][:50] + ("..." if len(task["message"]) > 50 else "")
        lines.append(f'• `{task["id"]}` - {time_str}: "{msg_preview}"')

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
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                try:
                    task_id = event["state_key"]
                    execute_at = datetime.fromisoformat(content["execute_at"])

                    # Only restore if still in the future
                    if execute_at > datetime.now(UTC):
                        task = asyncio.create_task(
                            _execute_scheduled_task(
                                client,
                                task_id,
                                content["room_id"],
                                content.get("thread_id"),
                                content["agent_user_id"],
                                execute_at,
                                content["message"],
                            ),
                        )
                        _running_tasks[task_id] = task
                        restored_count += 1

                except (KeyError, ValueError):
                    logger.exception("Failed to restore task")
                    continue

    if restored_count > 0:
        logger.info(f"Restored {restored_count} scheduled tasks in room {room_id}")

    return restored_count
