"""Tests for command parsing."""

from mindroom.commands import CommandType, command_parser, get_command_help


def test_invite_command_basic():
    """Test basic invite command parsing."""
    command = command_parser.parse("/invite calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"
    assert command.args["from_room"] is None
    assert command.args["duration_hours"] is None


def test_invite_command_with_at_symbol():
    """Test invite command with @ symbol."""
    command = command_parser.parse("/invite @calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"


def test_invite_command_with_room():
    """Test invite command with room specification."""
    command = command_parser.parse("/invite research from science")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "research"
    assert command.args["from_room"] == "science"
    assert command.args["duration_hours"] is None


def test_invite_command_with_room_hash():
    """Test invite command with room hash symbol."""
    command = command_parser.parse("/invite research from #science")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "research"
    assert command.args["from_room"] == "science"


def test_invite_command_with_duration():
    """Test invite command with duration."""
    command = command_parser.parse("/invite calculator for 2 hours")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"
    assert command.args["from_room"] is None
    assert command.args["duration_hours"] == 2


def test_invite_command_with_duration_variations():
    """Test invite command with different duration formats."""
    # Test "hour" singular
    command = command_parser.parse("/invite calculator for 1 hour")
    assert command is not None
    assert command.args["duration_hours"] == 1

    # Test "h" abbreviation
    command = command_parser.parse("/invite calculator for 3h")
    assert command is not None
    assert command.args["duration_hours"] == 3


def test_invite_command_full():
    """Test invite command with all parameters."""
    command = command_parser.parse("/invite @summary from #docs for 4 hours")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "summary"
    assert command.args["from_room"] == "docs"
    assert command.args["duration_hours"] == 4


def test_invite_command_case_insensitive():
    """Test invite command is case insensitive."""
    command = command_parser.parse("/INVITE calculator")
    assert command is not None
    assert command.type == CommandType.INVITE

    command = command_parser.parse("/Invite calculator FROM science FOR 2 HOURS")
    assert command is not None
    assert command.args["from_room"] == "science"


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
        "/invite calculator from",  # Incomplete from clause
        "/invite calculator for",  # Incomplete duration
        "/invite calculator for hours",  # Invalid duration format
        "invite calculator",  # Missing slash
        "just a regular message",
        "",
    ]

    for cmd_text in invalid_commands:
        command = command_parser.parse(cmd_text)
        assert command is None


def test_get_command_help():
    """Test help text generation."""
    # General help
    help_text = get_command_help()
    assert "Available Commands" in help_text
    assert "/invite" in help_text
    assert "/uninvite" in help_text
    assert "/list_invites" in help_text
    assert "/help" in help_text

    # Specific command help
    invite_help = get_command_help("invite")
    assert "Invite Command" in invite_help
    assert "Usage:" in invite_help
    assert "Examples:" in invite_help

    uninvite_help = get_command_help("uninvite")
    assert "Uninvite Command" in uninvite_help

    list_help = get_command_help("list_invites")
    assert "List Invites Command" in list_help
