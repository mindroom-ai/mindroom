"""Command parsing and handling for user commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import nio

from .logging_config import get_logger
from .matrix.client import get_room_members, invite_to_room
from .matrix.identity import MatrixID

if TYPE_CHECKING:
    from .config import Config
    from .thread_invites import ThreadInviteManager

logger = get_logger(__name__)


class CommandType(Enum):
    """Types of commands supported."""

    INVITE = "invite"
    UNINVITE = "uninvite"
    LIST_INVITES = "list_invites"
    HELP = "help"
    SCHEDULE = "schedule"
    LIST_SCHEDULES = "list_schedules"
    CANCEL_SCHEDULE = "cancel_schedule"
    WIDGET = "widget"


# Command documentation for each command type
COMMAND_DOCS = {
    CommandType.INVITE: ("!invite <agent>", "Invite an agent to the current thread"),
    CommandType.UNINVITE: ("!uninvite <agent>", "Remove an agent from the thread"),
    CommandType.LIST_INVITES: ("!list_invites", "Show all invited agents"),
    CommandType.SCHEDULE: ("!schedule <task>", "Schedule a task"),
    CommandType.LIST_SCHEDULES: ("!list_schedules", "List scheduled tasks"),
    CommandType.CANCEL_SCHEDULE: ("!cancel_schedule <id>", "Cancel a scheduled task"),
    CommandType.HELP: ("!help [topic]", "Get help"),
    CommandType.WIDGET: ("!widget [url]", "Add configuration widget"),
}


def get_command_list() -> str:
    """Get a formatted list of all available commands.

    Returns:
        Formatted string with all commands and their descriptions

    """
    lines = ["Available commands:"]
    for cmd_type in CommandType:
        if cmd_type in COMMAND_DOCS:
            syntax, description = COMMAND_DOCS[cmd_type]
            lines.append(f"- {syntax} - {description}")
    return "\n".join(lines)


@dataclass
class Command:
    """Parsed command with arguments."""

    type: CommandType
    args: dict[str, Any]
    raw_text: str


class CommandParser:
    """Parser for user commands in messages."""

    # Command patterns
    # Match: !invite agent
    INVITE_PATTERN = re.compile(
        r"^!invite\s+@?(\w+)$",  # agent name
        re.IGNORECASE,
    )
    UNINVITE_PATTERN = re.compile(r"^!uninvite\s+@?(\w+)$", re.IGNORECASE)
    LIST_INVITES_PATTERN = re.compile(r"^!list[_-]?invites?$", re.IGNORECASE)
    HELP_PATTERN = re.compile(r"^!help(?:\s+(.+))?$", re.IGNORECASE)
    SCHEDULE_PATTERN = re.compile(r"^!schedule\s+(.+)$", re.IGNORECASE | re.DOTALL)
    LIST_SCHEDULES_PATTERN = re.compile(r"^!list[_-]?schedules?$", re.IGNORECASE)
    CANCEL_SCHEDULE_PATTERN = re.compile(r"^!cancel[_-]?schedule\s+(.+)$", re.IGNORECASE)
    WIDGET_PATTERN = re.compile(r"^!widget(?:\s+(.+))?$", re.IGNORECASE)

    def parse(self, message: str) -> Command | None:  # noqa: PLR0911, C901
        """Parse a message for commands.

        Args:
            message: The message text to parse

        Returns:
            Parsed command or None if no command found

        """
        message = message.strip()

        # Handle voice emoji prefixes (e.g., "🎤 !schedule ...")
        voice_prefixes = ["🎤", "🎙️", "🗣️", "🎵"]
        for prefix in voice_prefixes:
            if message.startswith(prefix):
                # Remove the emoji and any whitespace, then continue parsing
                message = message[len(prefix) :].lstrip()
                break

        if not message.startswith("!"):
            return None

        # Try to match each command pattern

        # !invite command
        match = self.INVITE_PATTERN.match(message)
        if match:
            agent_name = match.group(1)
            args = {
                "agent_name": agent_name,
            }
            return Command(
                type=CommandType.INVITE,
                args=args,
                raw_text=message,
            )

        # !uninvite command
        match = self.UNINVITE_PATTERN.match(message)
        if match:
            agent_name = match.group(1)
            return Command(
                type=CommandType.UNINVITE,
                args={"agent_name": agent_name},
                raw_text=message,
            )

        # !list_invites command
        if self.LIST_INVITES_PATTERN.match(message):
            return Command(
                type=CommandType.LIST_INVITES,
                args={},
                raw_text=message,
            )

        # !help command
        match = self.HELP_PATTERN.match(message)
        if match:
            topic = match.group(1)
            return Command(
                type=CommandType.HELP,
                args={"topic": topic},
                raw_text=message,
            )

        # !schedule command
        match = self.SCHEDULE_PATTERN.match(message)
        if match:
            full_text = match.group(1).strip()
            # Pass the entire text to AI - it will parse both time and message
            return Command(
                type=CommandType.SCHEDULE,
                args={"full_text": full_text},
                raw_text=message,
            )

        # !list_schedules command
        if self.LIST_SCHEDULES_PATTERN.match(message):
            return Command(
                type=CommandType.LIST_SCHEDULES,
                args={},
                raw_text=message,
            )

        # !cancel_schedule command
        match = self.CANCEL_SCHEDULE_PATTERN.match(message)
        if match:
            task_id = match.group(1).strip()
            # Check if user wants to cancel all tasks
            cancel_all = task_id.lower() == "all"
            return Command(
                type=CommandType.CANCEL_SCHEDULE,
                args={"task_id": task_id, "cancel_all": cancel_all},
                raw_text=message,
            )

        # !widget command
        match = self.WIDGET_PATTERN.match(message)
        if match:
            url = match.group(1).strip() if match.group(1) else None
            return Command(
                type=CommandType.WIDGET,
                args={"url": url},
                raw_text=message,
            )

        # Unknown command
        logger.debug(f"Unknown command: {message}")
        return None


def get_command_help(topic: str | None = None) -> str:  # noqa: PLR0911
    """Get help text for commands.

    Args:
        topic: Specific topic to get help for (optional)

    Returns:
        Help text

    """
    if topic == "invite":
        return """**Invite Command**

Usage: `!invite <agent>` - Invite an agent to this thread

Example:
- `!invite calculator` - Invite calculator agent to this thread

Note: Invites only work in threads. The agent will be able to participate in this thread only.
Agents are automatically removed from the room 24 hours after being invited."""

    if topic == "uninvite":
        return """**Uninvite Command**

Usage: `!uninvite <agent>`

Example:
- `!uninvite calculator` - Remove calculator agent from this thread

The agent will no longer receive messages from this thread."""

    if topic in {"list", "list_invites"}:
        return """**List Invites Command**

Usage: `!list_invites` or `!listinvites`

Shows all agents currently invited to this thread."""

    if topic == "schedule":
        return """**Schedule Command**

Usage: `!schedule <time> <message>` - Schedule tasks, reminders, or agent workflows

**Simple Reminders:**
- `!schedule in 5 minutes Check the deployment`
- `!schedule tomorrow at 3pm Send the weekly report`
- `!schedule later Ping me about the meeting`
- `ping me tomorrow about the meeting`
- `remind me in 2 hours to review PRs`

**Event-Driven Workflows (New!):**
- `!schedule If I get an email about "urgent", @phone_agent call me`
- `!schedule When Bitcoin drops below $40k, @crypto_agent notify me`
- `!schedule If server CPU > 80%, @ops_agent scale up`
- `!schedule When someone mentions our product on Reddit, @analyst summarize it`
- `!schedule Whenever I get email from boss, @notification_agent alert me immediately`

**Agent Workflows:**
- `!schedule Daily at 9am, @finance give me a market analysis`
- `!schedule Every Monday, @research AI news and @email_assistant send me a summary`
- `!schedule tomorrow at 2pm, @email_assistant check my Gmail`

**Recurring Tasks (Cron-style):**
- `!schedule Every hour, @shell check server status`
- `!schedule Daily at 9am, @finance market report`
- `!schedule Weekly on Friday, @analyst prepare weekly summary`

How it works:
- **Time-based**: Executes at specific times or intervals
- **Event-based**: Automatically converts to smart polling (e.g., "if email" → check every 1-2 min)
- Agents receive clear instructions about conditions to check
- Multiple agents collaborate when mentioned together
- Automated tasks are clearly marked so agents don't wait for follow-up"""

    if topic == "list_schedules":
        return """**List Schedules Command**

Usage: `!list_schedules` or `!listschedules`

Shows all pending scheduled tasks in this thread."""

    if topic in {"cancel", "cancel_schedule"}:
        return """**Cancel Schedule Command**

Usage: `!cancel_schedule <id>` - Cancel a scheduled task
       `!cancel_schedule all` - Cancel ALL scheduled tasks in this room

Examples:
- `!cancel_schedule abc123` - Cancel the task with ID abc123
- `!cancel_schedule all` - Cancel all scheduled tasks (requires confirmation)

Use `!list_schedules` to see task IDs."""

    if topic == "widget":
        return """**Widget Command**

Usage: `!widget [url]` - Add the MindRoom configuration widget to this room

Examples:
- `!widget` - Add widget using default URL (http://localhost:3003)
- `!widget https://config.mindroom.ai` - Add widget from custom URL

The widget provides a visual interface for configuring MindRoom agents and settings.
Pin it to keep it visible in the room.

Note: Widget support requires Element Desktop or self-hosted Element Web."""

    # General help
    return """**Available Commands**

- `!invite <agent>` - Invite an agent to this thread
- `!uninvite <agent>` - Remove an agent from this thread
- `!list_invites` - List all invited agents
- `!schedule <time|condition> <message>` - Schedule time-based or event-driven workflows
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id|all>` - Cancel a scheduled task or all tasks
- `!widget [url]` - Add configuration widget to the room
- `!help [topic]` - Show this help or help for a specific command

**New Scheduling Features:**
- Recurring tasks with cron-style scheduling (daily, weekly, hourly)
- Agent workflows - mention agents to have them collaborate on scheduled tasks
- Natural language time parsing - "tomorrow", "in 5 minutes", "every Monday"

Note: All commands only work within threads, not in main room messages
(except !widget which works in the main room).

For detailed help on a command, use: `!help <command>`"""


async def handle_invite_command(
    room_id: str,
    thread_id: str,
    agent_name: str,
    sender: str,
    agent_domain: str,
    client: nio.AsyncClient,
    thread_invite_manager: ThreadInviteManager,
    config: Config,
) -> str:
    """Handle the invite command to invite an agent to a thread."""
    if agent_name not in config.agents:
        return f"❌ Unknown agent: @{agent_name}. Available agents: {', '.join(f'@{name}' for name in sorted(config.agents.keys()))}"

    # Add the thread invitation
    await thread_invite_manager.add_invite(thread_id, room_id, agent_name, sender)

    # Check if agent user exists in room
    agent_user_id = MatrixID.from_agent(agent_name, agent_domain).full_id
    room_members = await get_room_members(client, room_id)

    if isinstance(room_members, set):
        if agent_user_id not in room_members:
            # Invite the agent to the room (regular room invitation)
            success = await invite_to_room(client, room_id, agent_user_id)
            if success:
                logger.info("Invited agent to room", agent=agent_name, room_id=room_id)
            else:
                logger.error("Failed to invite agent to room", agent=agent_name, room_id=room_id)
    else:
        logger.error("Failed to get room members", room_id=room_id, error=str(room_members))

    response_text = f"✅ Invited @{agent_name} to this thread."
    response_text += f"\n\n@{agent_name}, you've been invited to help in this thread!"
    return response_text


async def handle_list_invites_command(
    room_id: str,
    thread_id: str,
    thread_invite_manager: ThreadInviteManager,
) -> str:
    """Handle the list invites command."""
    thread_invites = await thread_invite_manager.get_thread_agents(thread_id, room_id)
    if thread_invites:
        thread_list = "\n".join([f"- @{agent}" for agent in thread_invites])
        return f"**Invited agents in this thread:**\n{thread_list}"

    return "No agents are currently invited to this thread."


async def handle_widget_command(
    client: nio.AsyncClient,
    room_id: str,
    url: str | None = None,
) -> str:
    """Handle the widget command to add configuration widget to room.

    Args:
        client: The Matrix client
        room_id: The room ID to add widget to
        url: Optional custom widget URL

    Returns:
        Response text for the user

    """
    # Default URL for local development
    default_url = "http://localhost:3003/matrix-widget.html"
    widget_url = url if url else default_url

    # Create the widget state event content
    widget_content = {
        "type": "custom",
        "url": widget_url,
        "name": "MindRoom Configuration",
        "data": {"title": "MindRoom Configuration", "curl": widget_url.replace("/matrix-widget.html", "")},
        "creatorUserId": client.user_id,
        "id": "mindroom_config",
    }

    try:
        # Send the state event to add the widget
        response = await client.room_put_state(
            room_id=room_id,
            event_type="im.vector.modular.widgets",
            state_key="mindroom_config",
            content=widget_content,
        )

        if isinstance(response, nio.RoomPutStateError):
            logger.error(f"Failed to add widget to room {room_id}: {response.message}")
            return f"❌ Failed to add widget: {response.message}"

        logger.info(f"Successfully added widget to room {room_id}")
    except Exception as e:
        logger.exception("Error adding widget to room %s", room_id)
        return f"❌ Error adding widget: {e!s}"
    else:
        return (
            "✅ **MindRoom Configuration widget added!**\n\n"
            "• Pin the widget to keep it visible\n"
            "• All room members can access the configuration\n"
            "• Changes sync in real-time with config.yaml\n\n"
            f"Widget URL: {widget_url}\n\n"
            "**Note:** Widgets require Element Desktop or self-hosted Element Web.\n"
            "Alternatively, you can use: `/addwidget {url}` in Element."
        )


# Global parser instance
command_parser = CommandParser()
