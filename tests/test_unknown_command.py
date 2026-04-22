"""Tests for unknown command handling."""

from __future__ import annotations

import mindroom.tool_system.skills as skills_module
from mindroom.commands.parsing import CommandType, command_parser
from mindroom.response_runner import ResponseRunner


def test_unknown_command_parsing() -> None:
    """Test that unknown commands are parsed as UNKNOWN type."""
    # Test various unknown commands
    command = command_parser.parse("!invalid")
    assert command is not None
    assert command.type == CommandType.UNKNOWN
    assert command.args["raw_command"] == "!invalid"

    command = command_parser.parse("!notacommand")
    assert command is not None
    assert command.type == CommandType.UNKNOWN

    command = command_parser.parse("!test123")
    assert command is not None
    assert command.type == CommandType.UNKNOWN


def test_unknown_command_with_emoji() -> None:
    """Test that unknown commands with emoji prefixes are handled."""
    command = command_parser.parse("🎤 !invalidcommand")
    assert command is not None
    assert command.type == CommandType.UNKNOWN
    assert command.args["raw_command"] == "!invalidcommand"


def test_valid_commands_not_unknown() -> None:
    """Test that valid commands are not marked as unknown."""
    command = command_parser.parse("!help")
    assert command is not None
    assert command.type == CommandType.HELP

    command = command_parser.parse("!schedule daily")
    assert command is not None
    assert command.type == CommandType.SCHEDULE

    command = command_parser.parse("!skill demo")
    assert command is not None
    assert command.type == CommandType.UNKNOWN


def test_non_commands_return_none() -> None:
    """Test that non-commands still return None."""
    command = command_parser.parse("just a message")
    assert command is None

    command = command_parser.parse("invite without exclamation")
    assert command is None

    command = command_parser.parse("")
    assert command is None


def test_removed_skill_command_runtime_helpers_are_not_exposed() -> None:
    """The removed !skill feature should not leave command-specific runtime APIs behind."""
    assert not hasattr(skills_module, "resolve_skill_command_spec")
    assert not hasattr(ResponseRunner, "send_skill_command_response")
