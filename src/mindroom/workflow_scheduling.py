"""Workflow scheduling with AI-powered parsing and cron support."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from agno.agent import Agent
from cron_descriptor import get_description  # type: ignore[import-untyped]
from croniter import croniter  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from .ai import get_model_instance
from .logging_config import get_logger
from .matrix.client import send_message
from .matrix.identity import extract_server_name_from_homeserver
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

    def to_natural_language(self) -> str:
        """Convert cron schedule to natural language description.

        Examples:
        - "*/2 * * * *" → "Every 2 minutes"
        - "0 9 * * 1" → "At 09:00 AM, only on Monday"
        - "0 */4 * * *" → "Every 4 hours"

        """
        try:
            cron_str = self.to_cron_string()
            # Use cron-descriptor to get natural language
            return str(get_description(cron_str))
        except Exception:
            # Fallback to cron string if description fails
            return f"Cron: {self.to_cron_string()}"


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

IMPORTANT: Event-based and conditional requests:
When users say "if", "when", "whenever", "once X happens" or describe events/conditions:
1. Convert to an appropriate recurring (cron) schedule for polling
2. Include BOTH the condition check AND the action in the message
3. Choose polling frequency based on urgency and type:
   - Email/message checks: 1-5 minutes (urgent: 1 min, normal: 3-5 min)
   - System/server monitoring: 30 seconds - 5 minutes based on criticality
   - File/code changes: 1-5 minutes
   - Market/price data: 1-10 minutes based on volatility
   - Social media mentions: 5-15 minutes
   - General status checks: 5-15 minutes
4. The message should instruct agents to CHECK for the condition FIRST, then act only if true

Examples:

"Every Monday at 9am, research AI news and send me an email summary"
-> schedule_type: "cron"
   cron_schedule: {{minute: "0", hour: "9", day: "*", month: "*", weekday: "1"}}
   message: "@research @email_assistant Please research the latest AI news and developments from the past week, then send me an email summary of the most important findings."
   description: "Weekly AI news research and email"

"If I get an email about 'urgent', call me"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*/2", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@email_assistant Check for emails containing 'urgent' in subject or body. If found, @phone_agent please call the user immediately about the urgent email."
   description: "Monitor for urgent emails and alert"

"When someone mentions our product on Reddit, analyze it"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*/10", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points of the discussions."
   description: "Monitor Reddit mentions and analyze"

"If server CPU goes above 80%, scale up"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers immediately."
   description: "Monitor CPU and auto-scale"

"When the build fails, create a ticket"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*/5", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@ci_agent Check the latest build status. If failed, @ticket_agent create a high-priority ticket with the failure details."
   description: "Monitor builds and create failure tickets"

"If Bitcoin drops below $40k, notify me"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*/5", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user about the price drop."
   description: "Monitor Bitcoin price threshold"

"Whenever I get an email from my boss, notify me immediately"
-> schedule_type: "cron"
   cron_schedule: {{minute: "*", hour: "*", day: "*", month: "*", weekday: "*"}}
   message: "@email_assistant Check for new emails from boss. If any found, @notification_agent alert the user immediately."
   description: "Monitor for boss emails"

"Tomorrow at 3pm, check my Gmail"
-> schedule_type: "once"
   execute_at: [tomorrow at 15:00 UTC]
   message: "@email_assistant Please check my Gmail inbox and summarize any important messages"
   description: "One-time Gmail check"

"check my email in 15 seconds"
-> schedule_type: "once"
   execute_at: [current_time + 15 seconds]
   message: "@email_assistant Please check my email"
   description: "One-time email check"

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
- For conditional/event-based requests, ALWAYS include the check condition in the message
- The message should be natural and conversational
- Mention relevant agents with @ at the beginning if the task requires specific agents
- For simple reminders (ping, remind), don't mention agents unless specifically requested
- Be specific about what you want the agents to do if agents are mentioned
- For complex workflows, the agents will naturally collaborate when mentioned together
- Convert time expressions to UTC for the schedule, but DO NOT include them in the message
- IMPORTANT: Remove time references like "in 15 seconds", "tomorrow", "later" from the message itself
- The message is what gets posted WHEN the scheduled time arrives, so it should make sense at that moment
- For vague times like "tomorrow" without specific time, use 9:00 UTC as default
- For "later" or "soon", use 30 minutes from now
- CRITICAL: If schedule_type is "once", you MUST provide execute_at with a valid datetime
- CRITICAL: If schedule_type is "cron", you MUST provide cron_schedule with all fields filled
- If you cannot determine a specific time, default to 30 minutes from current_time for "once" tasks"""

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
            # Add fallback for missing required fields
            if result.schedule_type == "once" and not result.execute_at:
                logger.warning("One-time task missing execute_at, defaulting to 30 minutes from now")
                result.execute_at = current_time + timedelta(minutes=30)
            elif result.schedule_type == "cron" and not result.cron_schedule:
                logger.warning("Recurring task missing cron_schedule, defaulting to daily at 9am")
                result.cron_schedule = CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")

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
        # Build the message with clear automated task indicator
        # This helps agents understand it's not an interactive conversation
        automated_message = (
            f"⏰ [Automated Task]\n{workflow.message}\n\n_Note: Automated task - no follow-up expected._"
        )

        # Extract the server name from the client's homeserver for proper agent mentions
        server_name = extract_server_name_from_homeserver(client.homeserver)

        # Create mention content with the automated message
        content = create_mention_content_from_text(
            config,
            automated_message,
            sender_domain=server_name,
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
