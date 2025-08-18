"""Tests for command parsing with emoji prefixes."""

from __future__ import annotations

from mindroom.commands import CommandType, command_parser


def test_command_parser_with_voice_emoji() -> None:
    """Test that command parser handles voice emoji prefixes."""
    # Microphone emoji with schedule command
    command = command_parser.parse("🎤 !schedule in 10 minutes turn off lights")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "in 10 minutes turn off lights"

    # Microphone emoji with invite command
    command = command_parser.parse("🎤 !invite calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"


def test_command_parser_without_emoji() -> None:
    """Test that normal commands still work."""
    command = command_parser.parse("!invite calculator")
    assert command is not None
    assert command.type == CommandType.INVITE
    assert command.args["agent_name"] == "calculator"

    command = command_parser.parse("!schedule tomorrow meeting")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow meeting"


def test_non_commands_with_emoji() -> None:
    """Test that emoji-prefixed non-commands are not parsed."""
    # Voice emoji but no command
    command = command_parser.parse("🎤 just a regular message")
    assert command is None

    # Voice emoji with text that looks like a command but isn't
    command = command_parser.parse("🎤 invite someone to the party")
    assert command is None
