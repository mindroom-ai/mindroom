"""Scheduled task management with AI-powered natural language time parsing."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import nio
from agno.agent import Agent
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger

logger = get_logger(__name__)

# Event type for scheduled tasks in Matrix state
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"

# Global task storage for running asyncio tasks
_running_tasks: dict[str, asyncio.Task] = {}


class ScheduledTimeResponse(BaseModel):
    """Structured output for time parsing results."""

    execute_at: datetime = Field(description="The exact datetime when the task should execute (in UTC)")
    interpretation: str = Field(description="Human-readable explanation of the parsed time")


class ScheduleParseError(BaseModel):
    """Structured output when time parsing fails."""

    error: str = Field(description="Explanation of why the time couldn't be parsed")
    suggestion: str | None = Field(default=None, description="Suggestion for how to rephrase the time")


async def parse_schedule_time(
    time_expression: str, current_time: datetime | None = None
) -> ScheduledTimeResponse | ScheduleParseError:
    """Use AI with structured output to parse natural language time expressions."""
    if current_time is None:
        current_time = datetime.now(UTC)

    prompt = f"""Parse the following time expression into a specific datetime.

Current time (UTC): {current_time.isoformat()}Z
Time expression: "{time_expression}"

Convert this natural language time expression into an exact UTC datetime.

Examples of expressions you should handle:
- Relative: "in 5 minutes", "in 2 hours", "tomorrow", "next week"
- Absolute: "at 3pm", "tomorrow at 10am", "next Monday at 2pm"
- Contextual: "later", "in a bit", "soon" (use reasonable defaults like 30 minutes)

If you cannot parse the expression, return an error with a helpful suggestion."""

    try:
        # Use default model for simplicity
        model = get_model_instance("default")

        agent = Agent(
            name="TimeParser",
            role="Parse natural language time expressions",
            model=model,
            response_model=ScheduledTimeResponse,
        )

        response = await agent.arun(prompt, session_id=f"time_parse_{uuid.uuid4()}")
        result = response.content

        if isinstance(result, ScheduledTimeResponse):
            logger.info(
                "Successfully parsed time",
                expression=time_expression,
                execute_at=result.execute_at,
                interpretation=result.interpretation,
            )
            return result

    except Exception as e:
        logger.debug(f"Failed to parse as valid time, trying error response: {e}")

    # If we couldn't parse as valid time, try to get a helpful error
    try:
        model = get_model_instance("default")
        error_agent = Agent(
            name="TimeParser",
            role="Explain why time expression cannot be parsed",
            model=model,
            response_model=ScheduleParseError,
        )

        error_prompt = prompt + "\n\nProvide a clear error message and suggestion."
        error_response = await error_agent.arun(error_prompt, session_id=f"time_error_{uuid.uuid4()}")
        error_result = error_response.content

        if isinstance(error_result, ScheduleParseError):
            return error_result

    except Exception as e:
        logger.error("Failed to parse time expression", error=str(e))

    # Fallback error
    return ScheduleParseError(
        error="Unable to parse the time expression", suggestion="Try something like 'in 5 minutes' or 'tomorrow at 3pm'"
    )


async def schedule_task(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    agent_user_id: str,
    scheduled_by: str,
    time_expression: str,
    message: str,
) -> tuple[str | None, str]:
    """Schedule a task from natural language time expression.

    Returns:
        Tuple of (task_id, response_message)
    """
    # Parse the time expression
    parse_result = await parse_schedule_time(time_expression)

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
        "message": message,
        "status": "pending",
    }

    await client.room_put_state(
        room_id=room_id,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        content=task_data,
        state_key=task_id,
    )

    # Start the async task
    task = asyncio.create_task(
        _execute_scheduled_task(client, task_id, room_id, thread_id, agent_user_id, parse_result.execute_at, message)
    )
    _running_tasks[task_id] = task

    # Build success message
    success_msg = f"✅ {parse_result.interpretation}\n\n"
    success_msg += f'I\'ll send: "{message}"\n\n'
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

        await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        # Update task status to completed
        await client.room_put_state(
            room_id=room_id,
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            content={"status": "completed"},
            state_key=task_id,
        )

        # Clean up
        if task_id in _running_tasks:
            del _running_tasks[task_id]

    except asyncio.CancelledError:
        logger.info(f"Scheduled task {task_id} was cancelled")
        raise
    except Exception as e:
        logger.error(f"Failed to execute scheduled task {task_id}: {e}")


async def list_scheduled_tasks(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None = None,
) -> str:
    """List scheduled tasks in human-readable format."""
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return "Unable to retrieve scheduled tasks."

    tasks = []
    for event in response.events:
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE:
            content = event["content"]
            if content.get("status") == "pending":
                # Filter by thread if specified
                if thread_id and content.get("thread_id") != thread_id:
                    continue

                try:
                    task_info = {
                        "id": event["state_key"],
                        "time": datetime.fromisoformat(content["execute_at"]),
                        "message": content["message"],
                    }
                    tasks.append(task_info)
                except (KeyError, ValueError):
                    continue

    if not tasks:
        return "No scheduled tasks found."

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

    # Update state to cancelled
    try:
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

    except Exception as e:
        logger.error(f"Failed to cancel task {task_id}: {e}")
        return f"❌ Failed to cancel task `{task_id}`"


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
                            )
                        )
                        _running_tasks[task_id] = task
                        restored_count += 1

                except (KeyError, ValueError) as e:
                    logger.error(f"Failed to restore task: {e}")
                    continue

    if restored_count > 0:
        logger.info(f"Restored {restored_count} scheduled tasks in room {room_id}")

    return restored_count
