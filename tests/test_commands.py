"""Tests for command parsing."""

from mindroom.commands import CommandType, command_parser, get_command_help


def test_invite_command_basic():
    """Test basic invite command parsing."""
    command = command_parser.parse("/invite calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"


def test_invite_command_with_at_symbol():
    """Test invite command with @ symbol."""
    command = command_parser.parse("/invite @calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"


def test_invite_command_invalid_format():
    """Test invite command with invalid formats."""
    # Test with extra text (no longer supports duration)
    command = command_parser.parse("/invite calculator for 2 hours")
    assert command is None  # Should not parse with extra text


def test_invite_command_case_insensitive():
    """Test invite command is case insensitive."""
    command = command_parser.parse("/INVITE calculator")
    assert command is not None
    assert command.type == CommandType.INVITE

    # With extra text it should not parse
    command = command_parser.parse("/Invite calculator FOR 2 HOURS")
    assert command is None


def test_uninvite_command():
    """Test uninvite command parsing."""
    command = command_parser.parse("/uninvite calculator")
    assert command is not None
    assert command.type == CommandType.UNINVITE
    assert command.args["agent_name"] == "calculator"


def test_uninvite_command_with_at():
    """Test uninvite command with @ symbol."""
    command = command_parser.parse("/uninvite @research")
    assert command is not None
    assert command.type == CommandType.UNINVITE
    assert command.args["agent_name"] == "research"


def test_list_invites_command():
    """Test list invites command parsing."""
    # Test different variations
    variations = [
        "/list_invites",
        "/listinvites",
        "/list-invites",
        "/list_invite",  # singular
        "/LIST_INVITES",  # case insensitive
    ]

    for cmd_text in variations:
        command = command_parser.parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.LIST_INVITES
        assert command.args == {}


def test_help_command():
    """Test help command parsing."""
    # Basic help
    command = command_parser.parse("/help")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] is None

    # Help with topic
    command = command_parser.parse("/help invite")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] == "invite"


def test_invalid_commands():
    """Test that invalid commands return None."""
    invalid_commands = [
        "/invalid",
        "/invite",  # Missing agent name
        "/uninvite",  # Missing agent name
        "/invite calculator for",  # Incomplete duration
        "/invite calculator for hours",  # Invalid duration format
        "invite calculator",  # Missing slash
        "just a regular message",
        "",
    ]

    for cmd_text in invalid_commands:
        command = command_parser.parse(cmd_text)
        assert command is None


def test_schedule_command():
    """Test schedule command parsing."""
    # Basic schedule with time and message
    command = command_parser.parse("/schedule in 5 minutes Check the deployment")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "in 5 minutes Check the deployment"

    # Schedule with just time expression
    command = command_parser.parse("/schedule tomorrow")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow"

    # Schedule with complex expression
    command = command_parser.parse("/schedule tomorrow at 3pm Send the weekly report")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow at 3pm Send the weekly report"


def test_list_schedules_command():
    """Test list schedules command parsing."""
    variations = [
        "/list_schedules",
        "/listschedules",
        "/list-schedules",
        "/list_schedule",  # singular
        "/LIST_SCHEDULES",  # case insensitive
    ]

    for cmd_text in variations:
        command = command_parser.parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.LIST_SCHEDULES
        assert command.args == {}


def test_cancel_schedule_command():
    """Test cancel schedule command parsing."""
    # Basic cancel
    command = command_parser.parse("/cancel_schedule abc123")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "abc123"

    # With hyphen
    command = command_parser.parse("/cancel-schedule xyz789")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "xyz789"

    # Case insensitive
    command = command_parser.parse("/CANCEL_SCHEDULE task456")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE


def test_get_command_help():
    """Test help text generation."""
    # General help
    help_text = get_command_help()
    assert "Available Commands" in help_text
    assert "/invite" in help_text
    assert "/uninvite" in help_text
    assert "/list_invites" in help_text
    assert "/help" in help_text
    assert "/schedule" in help_text
    assert "/list_schedules" in help_text
    assert "/cancel_schedule" in help_text

    # Specific command help
    invite_help = get_command_help("invite")
    assert "Invite Command" in invite_help
    assert "Usage:" in invite_help
    assert "Example:" in invite_help  # Changed from "Examples:" to "Example:"

    uninvite_help = get_command_help("uninvite")
    assert "Uninvite Command" in uninvite_help

    list_help = get_command_help("list_invites")
    assert "List Invites Command" in list_help

    # Schedule command help
    schedule_help = get_command_help("schedule")
    assert "Schedule Command" in schedule_help
    assert "Examples:" in schedule_help
    assert "in 5 minutes" in schedule_help

    list_schedules_help = get_command_help("list_schedules")
    assert "List Schedules Command" in list_schedules_help

    cancel_help = get_command_help("cancel_schedule")
    assert "Cancel Schedule Command" in cancel_help
    assert "cancel_schedule" in cancel_help
