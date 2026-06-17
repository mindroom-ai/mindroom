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
    RELOAD_PLUGINS = "reload_plugins"
    SCHEDULE = "schedule"
    LIST_SCHEDULES = "list_schedules"
    CANCEL_SCHEDULE = "cancel_schedule"
    EDIT_SCHEDULE = "edit_schedule"
    CONFIG = "config"  # Configuration command
    MODEL = "model"  # Per-thread model override command
    THREAD_MODE = "thread_mode"  # Room-level thread mode override command
    HI = "hi"  # Welcome message command
    UNKNOWN = "unknown"  # Special type for unrecognized commands


# Command documentation for each command type
_COMMAND_DOCS = {
    CommandType.SCHEDULE: ("!schedule <task>", "Schedule a task"),
    CommandType.LIST_SCHEDULES: ("!list_schedules", "List scheduled tasks"),
    CommandType.CANCEL_SCHEDULE: ("!cancel_schedule <id>", "Cancel a scheduled task"),
    CommandType.EDIT_SCHEDULE: ("!edit_schedule <id> <task>", "Edit an existing scheduled task"),
    CommandType.HELP: ("!help [topic]", "Get help"),
    CommandType.RELOAD_PLUGINS: ("!reload-plugins", "Reload configured plugins (admin only)"),
    CommandType.CONFIG: ("!config <operation>", "Manage configuration"),
    CommandType.MODEL: ("!model [name|list|reset]", "Show or switch the model used in the current thread"),
    CommandType.THREAD_MODE: (
        "!thread_mode [room|thread|reset]",
        "Show or switch the thread mode used in the current room (room admin only)",
    ),
    CommandType.HI: ("!hi", "Show welcome message"),
}
_WELCOME_COMMAND_TYPES = (CommandType.HI, CommandType.SCHEDULE, CommandType.HELP)
_COMPACT_COMMAND_DOC_OVERRIDES = {
    CommandType.SCHEDULE: ("!schedule <time> <message>", "Schedule tasks and reminders"),
    CommandType.HELP: ("!help [topic]", "Get detailed help"),
    CommandType.HI: ("!hi", "Show this welcome message again"),
}


def _format_command_entry(syntax: str, description: str, *, format_code: bool = False, bullet: str = "-") -> str:
    command = f"`{syntax}`" if format_code else syntax
    return f"{bullet} {command} - {description}"


def _get_command_entries(format_code: bool = False) -> list[str]:
    """Get command entries as a list of formatted strings.

    Args:
        format_code: If True, wrap commands in backticks for markdown

    Returns:
        List of formatted command strings

    """
    return [
        _format_command_entry(*_COMMAND_DOCS[cmd_type], format_code=format_code)
        for cmd_type in CommandType
        if cmd_type in _COMMAND_DOCS and cmd_type != CommandType.UNKNOWN
    ]


def get_compact_command_entries(
    command_types: tuple[CommandType, ...] | None = None,
    *,
    format_code: bool = False,
) -> list[str]:
    """Get compact command entries for welcome and other short command lists."""
    return [
        _format_command_entry(
            *_COMPACT_COMMAND_DOC_OVERRIDES.get(cmd_type, _COMMAND_DOCS[cmd_type]),
            format_code=format_code,
            bullet="\u2022",
        )
        for cmd_type in (command_types or _WELCOME_COMMAND_TYPES)
    ]


@dataclass
class Command:
    """Parsed command with arguments."""

    type: CommandType
    args: dict[str, Any]
    raw_text: str


class _CommandParser:
    """Parser for user commands in messages."""

    HELP_PATTERN = re.compile(r"^!help(?:\s+(.+))?$", re.IGNORECASE)
    RELOAD_PLUGINS_PATTERN = re.compile(r"^!reload(?:-|_)plugins$", re.IGNORECASE)
    SCHEDULE_PATTERN = re.compile(r"^!schedule\s+(.+)$", re.IGNORECASE | re.DOTALL)
    LIST_SCHEDULES_PATTERN = re.compile(r"^!(?:list|inspect)[_-]?schedules?$", re.IGNORECASE)
    CANCEL_SCHEDULE_PATTERN = re.compile(r"^!cancel[_-]?schedule\s+(.+)$", re.IGNORECASE)
    EDIT_SCHEDULE_PATTERN = re.compile(r"^!edit[_-]?schedule\s+(\S+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
    CONFIG_PATTERN = re.compile(r"^!config(?:\s+(.+))?$", re.IGNORECASE)
    MODEL_PATTERN = re.compile(r"^!model(?:\s+(.+))?$", re.IGNORECASE)
    THREAD_MODE_PATTERN = re.compile(r"^!thread[_-]?mode(?:\s+(.+))?$", re.IGNORECASE)
    HI_PATTERN = re.compile(r"^!hi$", re.IGNORECASE)

    def parse(self, message: str) -> Command | None:  # noqa: C901, PLR0911
        """Parse a message for commands.

        Args:
            message: The message text to parse

        Returns:
            Parsed command or None if no command found

        """
        message = message.strip()

        message = message.removeprefix(VOICE_PREFIX)
        if not message.startswith("!"):
            return None

        if self.HI_PATTERN.match(message):
            return Command(
                type=CommandType.HI,
                args={},
                raw_text=message,
            )

        match = self.HELP_PATTERN.match(message)
        if match:
            topic = match.group(1)
            return Command(
                type=CommandType.HELP,
                args={"topic": topic},
                raw_text=message,
            )

        if self.RELOAD_PLUGINS_PATTERN.match(message):
            return Command(type=CommandType.RELOAD_PLUGINS, args={}, raw_text=message)

        match = self.SCHEDULE_PATTERN.match(message)
        if match:
            full_text = match.group(1).strip()
            return Command(
                type=CommandType.SCHEDULE,
                args={"full_text": full_text},
                raw_text=message,
            )

        if self.LIST_SCHEDULES_PATTERN.match(message):
            return Command(
                type=CommandType.LIST_SCHEDULES,
                args={},
                raw_text=message,
            )

        match = self.CANCEL_SCHEDULE_PATTERN.match(message)
        if match:
            task_id = match.group(1).strip()
            cancel_all = task_id.lower() == "all"
            return Command(
                type=CommandType.CANCEL_SCHEDULE,
                args={"task_id": task_id, "cancel_all": cancel_all},
                raw_text=message,
            )

        match = self.EDIT_SCHEDULE_PATTERN.match(message)
        if match:
            task_id = match.group(1).strip()
            full_text = match.group(2).strip()
            return Command(
                type=CommandType.EDIT_SCHEDULE,
                args={"task_id": task_id, "full_text": full_text},
                raw_text=message,
            )

        match = self.CONFIG_PATTERN.match(message)
        if match:
            args_text = match.group(1).strip() if match.group(1) else ""
            return Command(
                type=CommandType.CONFIG,
                args={"args_text": args_text},
                raw_text=message,
            )

        match = self.MODEL_PATTERN.match(message)
        if match:
            args_text = match.group(1).strip() if match.group(1) else ""
            return Command(
                type=CommandType.MODEL,
                args={"args_text": args_text},
                raw_text=message,
            )

        match = self.THREAD_MODE_PATTERN.match(message)
        if match:
            args_text = match.group(1).strip() if match.group(1) else ""
            return Command(
                type=CommandType.THREAD_MODE,
                args={"args_text": args_text},
                raw_text=message,
            )

        logger.debug("unknown_command", command=message)
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

Usage: `!schedule <time> <message>` - Schedule tasks, reminders, or agent/team workflows

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

**Agent and Team Workflows:**
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
- Agents and teams receive clear instructions about conditions to check
- Multiple agents collaborate when mentioned together; mention a team directly for its team workflow
- Automated tasks are clearly marked and agents or teams follow up when they fire"""

    if topic in {"reload-plugins", "reload_plugins"}:
        return """**Reload Plugins Command**

Usage: `!reload-plugins` - Force-reload all configured plugins from disk

Alternative syntax: `!reload_plugins`

Notes:
- Admin only. Caller must be in `authorization.global_users`.
- Use this when you want to force a plugin reload immediately instead of waiting for the file watcher.
- The reply shows the active plugin set and the count of cancelled background tasks."""

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

**Permission:** Disabled by default.
Set `authorization.config_command_enabled: true`; caller must also be in `authorization.global_users`.

**Note:** Configuration changes are immediately saved to config.yaml and affect all new agent interactions."""

    if topic == "model":
        return """**Model Command**

Usage: `!model [name|list|reset]` - Show or switch the model used in the current thread

**Examples:**
- `!model` or `!model list` - Show the current thread's model override and the available models
- `!model opus` - Make every agent and team in this thread use the `opus` model
- `!model reset` - Remove the override so agents use their configured models again

How it works:
- The override applies to all agents, teams, and the router from the next message in the thread
- Model names come from the `models:` section of config.yaml
- The override is scoped to one thread and survives restarts; other threads and rooms are unaffected
- Room-wide overrides are configured separately via `room_models` in config.yaml"""

    if topic == "thread_mode":
        return """**Thread Mode Command**

Usage: `!thread_mode [room|thread|reset|show]` - Show or switch the thread mode used in the current room

**Examples:**
- `!thread_mode` or `!thread_mode show` - Show the current room override
- `!thread_mode room` - Use one continuous conversation for the whole room
- `!thread_mode thread` - Use Matrix threads for separate conversations in this room
- `!thread_mode reset` - Remove the room override and use configured modes again

How it works:
- The override applies to future agent replies in the current Matrix room
- The override is stored in MindRoom runtime state, not config.yaml
- Only Matrix room admins can set or reset the override"""

    # General help - dynamically generated from COMMAND_DOCS
    commands_text = "\n".join(_get_command_entries(format_code=True))

    return f"""**Available Commands**

{commands_text}

**Scheduling Features:**
- Time-based and event-driven workflows
- Recurring tasks with cron-style scheduling (daily, weekly, hourly)
- Agent and team workflows - mention multiple agents for ad-hoc collaboration, or mention a team for its team workflow
- Natural language time parsing - "tomorrow", "in 5 minutes", "every Monday"

For detailed help on a command, use: `!help <command>`"""


# Global parser instance
command_parser = _CommandParser()
