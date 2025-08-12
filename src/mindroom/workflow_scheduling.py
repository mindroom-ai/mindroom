"""Workflow scheduling with AI-powered parsing and cron support."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from agno.agent import Agent
from croniter import croniter  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger
from .matrix.client import send_message
from .matrix.mentions import create_mention_content_from_text

if TYPE_CHECKING:
    import nio

    from .config import Config

logger = get_logger(__name__)


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


class ScheduledWorkflow(BaseModel):
    """Structured representation of a scheduled task or workflow."""

    # Scheduling
    schedule_type: Literal["once", "cron"] = Field(description="One-time or recurring")
    execute_at: datetime | None = Field(default=None, description="For one-time tasks")
    cron_schedule: CronSchedule | None = Field(default=None, description="For recurring tasks")

    # The message to post (with agent mentions)
    message: str = Field(description="The message to post in the thread, including @agent mentions")

    # Metadata
    description: str = Field(description="Human-readable description of this workflow")
    created_by: str | None = Field(default=None, description="User who created this schedule")
    thread_id: str | None = Field(default=None, description="Thread where this will execute")
    room_id: str | None = Field(default=None, description="Room containing the thread")


class WorkflowParseError(BaseModel):
    """Error response when workflow parsing fails."""

    error: str = Field(description="Explanation of why the workflow couldn't be parsed")
    suggestion: str | None = Field(default=None, description="Suggestion for how to fix the issue")


async def parse_workflow_schedule(
    request: str,
    config: Config,
    current_time: datetime | None = None,
) -> ScheduledWorkflow | WorkflowParseError:
    """Parse natural language into structured workflow using AI.

    The key insight: we don't execute agents directly. Instead, we post
    a message mentioning them, and they collaborate naturally!

    Examples:
    - "Every Monday, research AI news and email me a summary"
      -> Posts: "@research @email_assistant Find the latest AI news and email me a summary"

    - "Daily at 9am, give me a market analysis"
      -> Posts: "@finance Please provide a market analysis for today"

    - "In 2 hours, check my Gmail"
      -> Posts: "@email_assistant Check my Gmail for important messages"

    """
    if current_time is None:
        current_time = datetime.now(UTC)

    # Get available agents for the prompt
    agent_list = ", ".join(f"@{name}" for name in config.agents)

    prompt = f"""Parse this scheduling request into a structured workflow.

Current time (UTC): {current_time.isoformat()}Z
Request: "{request}"

Your task is to:
1. Determine if this is a one-time task or recurring (cron)
2. Extract the schedule/timing
3. Create a message that mentions the appropriate agents

Available agents: {agent_list}

Examples:

"Every Monday at 9am, research AI news and send me an email summary"
-> schedule_type: "cron"
   cron_schedule: {{minute: "0", hour: "9", day: "*", month: "*", weekday: "1"}}
   message: "@research @email_assistant Please research the latest AI news and developments from the past week, then send me an email summary of the most important findings."
   description: "Weekly AI news research and email"

"Tomorrow at 3pm, check my Gmail"
-> schedule_type: "once"
   execute_at: [tomorrow at 15:00 UTC]
   message: "@email_assistant Please check my Gmail inbox and summarize any important messages"
   description: "One-time Gmail check"

"Every hour, check server status"
-> schedule_type: "cron"
   cron_schedule: {{minute: "0", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@shell Please check the server status with 'systemctl status'"
   description: "Hourly server status check"

"Daily at 9am, market analysis"
-> schedule_type: "cron"
   cron_schedule: {{minute: "0", hour: "9", day: "*", month: "*", weekday: "*"}}
   message: "@finance Please provide a comprehensive market analysis including major indices, notable movers, and key economic events for today"
   description: "Daily market analysis"

"ping me in 5 minutes to check the deployment"
-> schedule_type: "once"
   execute_at: [current_time + 5 minutes]
   message: "Check the deployment status"
   description: "Deployment check reminder"

"remind me tomorrow about the meeting"
-> schedule_type: "once"
   execute_at: [tomorrow at 09:00 UTC]
   message: "Reminder: You have a meeting today"
   description: "Meeting reminder"

Important rules:
- The message should be natural and conversational
- Mention relevant agents with @ at the beginning if the task requires specific agents
- For simple reminders (ping, remind), don't mention agents unless specifically requested
- Be specific about what you want the agents to do if agents are mentioned
- For complex workflows, the agents will naturally collaborate when mentioned together
- Convert time expressions to UTC
- For vague times like "tomorrow" without specific time, use 9:00 UTC as default
- For "later" or "soon", use 30 minutes from now"""

    model = get_model_instance(config, "default")

    agent = Agent(
        name="WorkflowParser",
        role="Parse scheduling requests into structured workflows",
        model=model,
        response_model=ScheduledWorkflow,
    )

    try:
        response = await agent.arun(prompt, session_id=f"workflow_parse_{uuid.uuid4()}")
        result = response.content

        if isinstance(result, ScheduledWorkflow):
            logger.info(
                "Successfully parsed workflow schedule",
                request=request,
                schedule_type=result.schedule_type,
                description=result.description,
            )
            return result

        # If we somehow get a different type, convert to error
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
    """Execute a scheduled workflow by posting its message to the thread.

    This is beautifully simple - we just post a message mentioning agents,
    and they handle the rest through normal collaboration!
    """
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return

    try:
        # Build the message content
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"⏰ Scheduled task: {workflow.message}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"⏰ <em>Scheduled task:</em> {workflow.message}",
        }

        # Add thread relation if specified
        if workflow.thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": workflow.thread_id,
            }
        content = create_mention_content_from_text(
            config,
            f"⏰ Scheduled task: {workflow.message}",
            thread_event_id=workflow.thread_id,
        )
        # Send the message - agents will see their mentions and respond naturally!
        await send_message(client, workflow.room_id, content)

        logger.info(
            "Executed scheduled workflow",
            description=workflow.description,
            thread_id=workflow.thread_id,
        )

    except Exception as e:
        logger.exception("Failed to execute scheduled workflow")

        # Send error notification
        error_content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"❌ Scheduled task failed: {workflow.description}\nError: {e!s}",
        }
        if workflow.thread_id:
            error_content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": workflow.thread_id,
            }
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
            # Calculate next run time
            next_run = cron.get_next(datetime)
            delay = (next_run - datetime.now(UTC)).total_seconds()

            if delay > 0:
                logger.info(
                    f"Waiting {delay:.0f} seconds until next execution",
                    task_id=task_id,
                    next_run=next_run.isoformat(),
                )
                await asyncio.sleep(delay)

            # Execute the workflow
            await execute_scheduled_workflow(client, workflow, config)

            # Check if task still exists (might have been cancelled)
            if task_id not in running_tasks:
                logger.info(f"Task {task_id} no longer in running tasks, stopping")
                break

    except asyncio.CancelledError:
        logger.info(f"Cron task {task_id} was cancelled")
        raise
    except Exception:
        logger.exception(f"Error in cron task {task_id}")


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
        # Calculate delay
        delay = (workflow.execute_at - datetime.now(UTC)).total_seconds()
        if delay > 0:
            logger.info(
                f"Waiting {delay:.0f} seconds until execution",
                task_id=task_id,
                execute_at=workflow.execute_at.isoformat(),
            )
            await asyncio.sleep(delay)

        # Execute the workflow
        await execute_scheduled_workflow(client, workflow, config)

    except asyncio.CancelledError:
        logger.info(f"One-time task {task_id} was cancelled")
        raise
    except Exception:
        logger.exception(f"Error in one-time task {task_id}")
