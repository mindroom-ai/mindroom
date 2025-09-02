"""Tests for command parsing."""

from __future__ import annotations

from mindroom.commands import COMMAND_DOCS, CommandType, command_parser, get_command_help


def test_help_command() -> None:
    """Test help command parsing."""
    # Basic help
    command = command_parser.parse("!help")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] is None

    # Help with topic
    command = command_parser.parse("!help invite")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] == "invite"


def test_hi_command() -> None:
    """Test hi command parsing."""
    # Basic hi command
    command = command_parser.parse("!hi")
    assert command is not None
    assert command.type == CommandType.HI
    assert command.args == {}

    # Case insensitive
    command = command_parser.parse("!HI")
    assert command is not None
    assert command.type == CommandType.HI

    # With trailing space (should still work)
    command = command_parser.parse("!hi ")
    assert command is not None
    assert command.type == CommandType.HI


def test_invalid_commands() -> None:
    """Test that invalid commands are handled correctly."""
    # Commands that should return UNKNOWN
    unknown_commands = [
        "!invalid",
        "!unknowncmd",
        "!test123",
        "!notacommand",
    ]

    for cmd_text in unknown_commands:
        command = command_parser.parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.UNKNOWN

    # Non-commands that should return None
    non_commands = [
        "invite calculator",  # Missing exclamation
        "just a regular message",
        "",
    ]

    for cmd_text in non_commands:
        command = command_parser.parse(cmd_text)
        assert command is None


def test_schedule_command() -> None:
    """Test schedule command parsing."""
    # Basic schedule with time and message
    command = command_parser.parse("!schedule in 5 minutes Check the deployment")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "in 5 minutes Check the deployment"

    # Schedule with just time expression
    command = command_parser.parse("!schedule tomorrow")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow"

    # Schedule with complex expression
    command = command_parser.parse("!schedule tomorrow at 3pm Send the weekly report")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow at 3pm Send the weekly report"


def test_list_schedules_command() -> None:
    """Test list schedules command parsing."""
    variations = [
        "!list_schedules",
        "!listschedules",
        "!list-schedules",
        "!list_schedule",  # singular
        "!LIST_SCHEDULES",  # case insensitive
    ]

    for cmd_text in variations:
        command = command_parser.parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.LIST_SCHEDULES
        assert command.args == {}


def test_all_commands_have_documentation() -> None:
    """Test that all CommandType values have documentation."""
    # Check that all commands have documentation (except UNKNOWN which is special)
    commands_needing_docs = set(CommandType) - {CommandType.UNKNOWN}
    missing_docs = commands_needing_docs - set(COMMAND_DOCS.keys())
    assert not missing_docs, f"Missing documentation for commands: {missing_docs}"

    # Check that there are no extra documentation entries
    extra_docs = set(COMMAND_DOCS.keys()) - set(CommandType)
    assert not extra_docs, f"Documentation for non-existent commands: {extra_docs}"

    # Check that all documentation entries are properly formatted
    for cmd_type, (syntax, description) in COMMAND_DOCS.items():
        assert syntax.startswith("!"), f"{cmd_type} syntax should start with '!'"
        assert len(description) > 0, f"{cmd_type} should have a description"


def test_cancel_schedule_command() -> None:
    """Test cancel schedule command parsing."""
    # Basic cancel
    command = command_parser.parse("!cancel_schedule abc123")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "abc123"
    assert command.args["cancel_all"] is False

    # With hyphen
    command = command_parser.parse("!cancel-schedule xyz789")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "xyz789"
    assert command.args["cancel_all"] is False

    # Case insensitive
    command = command_parser.parse("!CANCEL_SCHEDULE task456")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["cancel_all"] is False

    # Cancel all tasks
    command = command_parser.parse("!cancel_schedule all")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "all"
    assert command.args["cancel_all"] is True

    # Cancel all with different case
    command = command_parser.parse("!cancel_schedule ALL")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "ALL"
    assert command.args["cancel_all"] is True


def test_get_command_help() -> None:
    """Test help text generation."""
    # General help
    help_text = get_command_help()
    assert "Available Commands" in help_text
    assert "!schedule" in help_text
    assert "!widget" in help_text
    assert "!help" in help_text
    assert "!schedule" in help_text
    assert "!list_schedules" in help_text
    assert "!cancel_schedule" in help_text

    # Specific command help
    schedule_help = get_command_help("schedule")
    assert "Schedule Command" in schedule_help
    assert "Usage:" in schedule_help
    assert "Reminders" in schedule_help or "Workflows" in schedule_help

    widget_help = get_command_help("widget")
    assert "Widget Command" in widget_help

    # Schedule command help
    schedule_help = get_command_help("schedule")
    assert "Schedule Command" in schedule_help
    assert "Simple Reminders:" in schedule_help
    assert "Agent Workflows:" in schedule_help
    assert "in 5 minutes" in schedule_help

    list_schedules_help = get_command_help("list_schedules")
    assert "List Schedules Command" in list_schedules_help

    cancel_help = get_command_help("cancel_schedule")
    assert "Cancel Schedule Command" in cancel_help
    assert "cancel_schedule" in cancel_help
