"""Command parsing and handling for user commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mindroom.constants import VOICE_PREFIX
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


class CommandType(Enum):
    """Types of commands supported."""

    HELP = "help"
    SCHEDULE = "schedule"
    LIST_SCHEDULES = "list_schedules"
    CANCEL_SCHEDULE = "cancel_schedule"
    EDIT_SCHEDULE = "edit_schedule"
    CONFIG = "config"  # Configuration command
    HI = "hi"  # Welcome message command
    SKILL = "skill"  # Skill command
    UNKNOWN = "unknown"  # Special type for unrecognized commands


# Command documentation for each command type
_COMMAND_DOCS = {
    CommandType.SCHEDULE: ("!schedule <task>", "Schedule a task"),
    CommandType.LIST_SCHEDULES: ("!list_schedules", "List scheduled tasks"),
    CommandType.CANCEL_SCHEDULE: ("!cancel_schedule <id>", "Cancel a scheduled task"),
    CommandType.EDIT_SCHEDULE: ("!edit_schedule <id> <task>", "Edit an existing scheduled task"),
    CommandType.HELP: ("!help [topic]", "Get help"),
    CommandType.CONFIG: ("!config <operation>", "Manage configuration"),
    CommandType.HI: ("!hi", "Show welcome message"),
    CommandType.SKILL: ("!skill <name> [args]", "Run a skill by name"),
}


def _get_command_entries(format_code: bool = False) -> list[str]:
    """Get command entries as a list of formatted strings.

    Args:
        format_code: If True, wrap commands in backticks for markdown

    Returns:
        List of formatted command strings

    """
    entries = []
    for cmd_type in CommandType:
        if cmd_type in _COMMAND_DOCS and cmd_type != CommandType.UNKNOWN:
            syntax, description = _COMMAND_DOCS[cmd_type]
            if format_code:
                entries.append(f"- `{syntax}` - {description}")
            else:
                entries.append(f"- {syntax} - {description}")
    return entries


def get_command_list() -> str:
    """Get a formatted list of all available commands.

    Returns:
        Formatted string with all commands and their descriptions

    """
    lines = ["Available commands:", *_get_command_entries(format_code=False)]
    return "\n".join(lines)


@dataclass
class Command:
    """Parsed command with arguments."""

    type: CommandType
    args: dict[str, Any]
    raw_text: str


class _CommandParser:
    """Parser for user commands in messages."""

    # Command patterns
    HELP_PATTERN = re.compile(r"^!help(?:\s+(.+))?$", re.IGNORECASE)
    SCHEDULE_PATTERN = re.compile(r"^!schedule\s+(.+)$", re.IGNORECASE | re.DOTALL)
    LIST_SCHEDULES_PATTERN = re.compile(r"^!(?:list|inspect)[_-]?schedules?$", re.IGNORECASE)
    CANCEL_SCHEDULE_PATTERN = re.compile(r"^!cancel[_-]?schedule\s+(.+)$", re.IGNORECASE)
    EDIT_SCHEDULE_PATTERN = re.compile(r"^!edit[_-]?schedule\s+(\S+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
    CONFIG_PATTERN = re.compile(r"^!config(?:\s+(.+))?$", re.IGNORECASE)
    HI_PATTERN = re.compile(r"^!hi$", re.IGNORECASE)
    SKILL_PATTERN = re.compile(r"^!skill(?:\s+(.+))?$", re.IGNORECASE)

    def parse(self, message: str) -> Command | None:  # noqa: C901, PLR0911
        """Parse a message for commands.

        Args:
            message: The message text to parse

        Returns:
            Parsed command or None if no command found

        """
        message = message.strip()

        # Handle voice emoji prefixe (e.g., "🎤 !schedule ...")
        message = message.removeprefix(VOICE_PREFIX)
        if not message.startswith("!"):
            return None

        # Try to match each command pattern

        # !hi command (check this early as it's simple)
        if self.HI_PATTERN.match(message):
            return Command(
                type=CommandType.HI,
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

        # !edit_schedule command
        match = self.EDIT_SCHEDULE_PATTERN.match(message)
        if match:
            task_id = match.group(1).strip()
            full_text = match.group(2).strip()
            return Command(
                type=CommandType.EDIT_SCHEDULE,
                args={"task_id": task_id, "full_text": full_text},
                raw_text=message,
            )

        # !config command
        match = self.CONFIG_PATTERN.match(message)
        if match:
            args_text = match.group(1).strip() if match.group(1) else ""
            return Command(
                type=CommandType.CONFIG,
                args={"args_text": args_text},
                raw_text=message,
            )

        # !skill command
        match = self.SKILL_PATTERN.match(message)
        if match:
            payload = match.group(1).strip() if match.group(1) else ""
            if not payload:
                return Command(
                    type=CommandType.SKILL,
                    args={"skill_name": None, "args_text": ""},
                    raw_text=message,
                )
            parts = payload.split(maxsplit=1)
            skill_name = parts[0].strip()
            args_text = parts[1].strip() if len(parts) > 1 else ""
            return Command(
                type=CommandType.SKILL,
                args={"skill_name": skill_name, "args_text": args_text},
                raw_text=message,
            )

        # Unknown command - return a special Command indicating it's unknown
        logger.debug(f"Unknown command: {message}")
        return Command(
            type=CommandType.UNKNOWN,
            args={"raw_command": message},
            raw_text=message,
        )


def get_command_help(topic: str | None = None) -> str:  # noqa: PLR0911
    """Get help text for commands.

    Args:
        topic: Specific topic to get help for (optional)

    Returns:
        Help text

    """
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

    if topic == "skill":
        return """**Skill Command**

Usage: `!skill <name> [args]` - Run a user-invocable skill by name

Examples:
- `!skill repo-quick-audit`
- `!skill summarize Release notes for v2.3`

Notes:
- Skills must be enabled on the target agent and marked `user-invocable: true`.
- When a skill uses `command-dispatch: tool`, the tool runs directly with raw args."""

    if topic in {"list_schedules", "inspect_schedules"}:
        return """**List Schedules Command**

Usage: `!list_schedules`

Alternative syntax: `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`

Shows pending scheduled tasks. When used in a thread, shows tasks for that thread. When used in the main room, shows all tasks in the room."""

    if topic in {"cancel", "cancel_schedule"}:
        return """**Cancel Schedule Command**

Usage: `!cancel_schedule <id>` - Cancel a scheduled task
       `!cancel_schedule all` - Cancel ALL scheduled tasks in this room

Alternative syntax: `!cancelschedule`, `!cancel-schedule`

Examples:
- `!cancel_schedule abc123` - Cancel the task with ID abc123
- `!cancel_schedule all` - Cancel all scheduled tasks

Use `!list_schedules` to see task IDs."""

    if topic in {"edit", "edit_schedule"}:
        return """**Edit Schedule Command**

Usage: `!edit_schedule <id> <new task>` - Replace an existing scheduled task with new timing/content

Alternative syntax: `!editschedule`, `!edit-schedule`

Examples:
- `!edit_schedule abc123 tomorrow at 9am @finance send market update`
- `!edit_schedule task42 every weekday at 8am check build status`

Use `!list_schedules` to find task IDs before editing."""

    if topic == "config":
        return """**Config Command**

Usage: `!config <operation>` - View and modify MindRoom configuration

**Viewing Configuration:**
- `!config show` - Show entire configuration
- `!config get <path>` - Get a specific configuration value
- `!config get agents` - Show all agents
- `!config get models.default` - Show default model
- `!config get agents.analyst.display_name` - Show analyst's display name

**Modifying Configuration:**
- `!config set <path> <value>` - Set a configuration value
- `!config set agents.analyst.display_name "Research Expert"` - Change display name
- `!config set models.default.id gpt-4` - Change default model
- `!config set defaults.markdown false` - Disable markdown by default
- `!config set timezone America/New_York` - Set timezone

**Path Syntax:**
- Use dot notation to navigate nested config (e.g., `agents.analyst.role`)
- Arrays use indexes (e.g., `agents.analyst.tools.0` for first tool)
- String values with spaces must be quoted

**Note:** Configuration changes are immediately saved to config.yaml and affect all new agent interactions."""

    # General help - dynamically generated from COMMAND_DOCS
    commands_text = "\n".join(_get_command_entries(format_code=True))

    return f"""**Available Commands**

{commands_text}

**Scheduling Features:**
- Time-based and event-driven workflows
- Recurring tasks with cron-style scheduling (daily, weekly, hourly)
- Agent workflows - mention agents to have them collaborate on scheduled tasks
- Natural language time parsing - "tomorrow", "in 5 minutes", "every Monday"

Note: All commands only work within threads, not in main room messages.

For detailed help on a command, use: `!help <command>`"""


# Global parser instance
command_parser = _CommandParser()
