"""Tests for configuration commands."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mindroom import constants as constants_mod
from mindroom.commands.config_commands import (
    _format_value,
    _get_nested_value,
    _parse_config_args,
    _parse_value,
    _set_nested_value,
    handle_config_command,
)
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, _CommandParser
from mindroom.constants import resolve_runtime_paths


def _runtime_paths_for_config(config_path: Path) -> constants_mod.RuntimePaths:
    return resolve_runtime_paths(config_path=config_path)


class TestCommandParser:
    """Test config command parsing."""

    def test_parse_config_empty(self) -> None:
        """Test parsing !config with no args."""
        parser = _CommandParser()
        command = parser.parse("!config")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == ""

    def test_parse_config_show(self) -> None:
        """Test parsing !config show command."""
        parser = _CommandParser()
        command = parser.parse("!config show")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == "show"

    def test_parse_config_get(self) -> None:
        """Test parsing !config get command."""
        parser = _CommandParser()
        command = parser.parse("!config get agents.analyst.display_name")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == "get agents.analyst.display_name"

    def test_parse_config_set(self) -> None:
        """Test parsing !config set command."""
        parser = _CommandParser()
        command = parser.parse('!config set agents.analyst.display_name "New Name"')
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == 'set agents.analyst.display_name "New Name"'


class TestConfigArgsParsing:
    """Test config command argument parsing."""

    def test_parse_empty_args(self) -> None:
        """Test parsing empty config args defaults to show."""
        operation, args = _parse_config_args("")
        assert operation == "show"
        assert args == []

    def test_parse_show_operation(self) -> None:
        """Test parsing show operation."""
        operation, args = _parse_config_args("show")
        assert operation == "show"
        assert args == []

    def test_parse_get_operation(self) -> None:
        """Test parsing get operation with path."""
        operation, args = _parse_config_args("get agents.analyst")
        assert operation == "get"
        assert args == ["agents.analyst"]

    def test_parse_set_operation_simple(self) -> None:
        """Test parsing set operation with simple value."""
        operation, args = _parse_config_args("set defaults.markdown false")
        assert operation == "set"
        assert args == ["defaults.markdown", "false"]

    def test_parse_set_operation_quoted(self) -> None:
        """Test parsing set operation with quoted string."""
        operation, args = _parse_config_args('set agents.analyst.display_name "Research Expert"')
        assert operation == "set"
        assert args == ["agents.analyst.display_name", "Research Expert"]

    def test_parse_unmatched_quotes(self) -> None:
        """Test parsing with unmatched quotes returns parse_error."""
        operation, args = _parse_config_args('set test.value "unmatched')
        assert operation == "parse_error"
        assert len(args) == 1
        assert "closing quotation" in args[0].lower()

    def test_parse_mismatched_quotes(self) -> None:
        """Test parsing with mismatched quotes returns parse_error."""
        operation, args = _parse_config_args("set test.value 'mismatched\"")
        assert operation == "parse_error"
        assert len(args) == 1
        assert "closing quotation" in args[0].lower()


class TestNestedValueOperations:
    """Test nested value get/set operations."""

    def test_get_nested_simple(self) -> None:
        """Test getting simple nested value."""
        data = {"agents": {"analyst": {"display_name": "Analyst"}}}
        value = _get_nested_value(data, "agents.analyst.display_name")
        assert value == "Analyst"

    def test_get_nested_list(self) -> None:
        """Test getting value from list."""
        data = {"tools": ["tool1", "tool2", "tool3"]}
        value = _get_nested_value(data, "tools.1")
        assert value == "tool2"

    def test_get_nested_nonexistent(self) -> None:
        """Test getting nonexistent path raises KeyError."""
        data = {"agents": {}}
        with pytest.raises(KeyError):
            _get_nested_value(data, "agents.analyst.display_name")

    def test_set_nested_simple(self) -> None:
        """Test setting simple nested value."""
        data = {"agents": {"analyst": {"display_name": "Old"}}}
        _set_nested_value(data, "agents.analyst.display_name", "New")
        assert data["agents"]["analyst"]["display_name"] == "New"

    def test_set_nested_create_intermediate(self) -> None:
        """Test setting creates intermediate dicts."""
        data = {"agents": {}}
        _set_nested_value(data, "agents.analyst.display_name", "Analyst")
        assert data["agents"]["analyst"]["display_name"] == "Analyst"

    def test_set_nested_list(self) -> None:
        """Test setting value in list."""
        data = {"tools": ["tool1", "tool2", "tool3"]}
        _set_nested_value(data, "tools.1", "new_tool")
        assert data["tools"][1] == "new_tool"


class TestValueParsing:
    """Test value parsing from strings."""

    def test_parse_boolean_true(self) -> None:
        """Test parsing true boolean."""
        assert _parse_value("true") is True
        assert _parse_value("True") is True

    def test_parse_boolean_false(self) -> None:
        """Test parsing false boolean."""
        assert _parse_value("false") is False
        assert _parse_value("False") is False

    def test_parse_none(self) -> None:
        """Test parsing None/null."""
        assert _parse_value("null") is None

    def test_parse_integer(self) -> None:
        """Test parsing integer."""
        assert _parse_value("42") == 42
        assert _parse_value("-10") == -10

    def test_parse_float(self) -> None:
        """Test parsing float."""
        assert _parse_value("3.14") == 3.14
        assert _parse_value("-0.5") == -0.5

    def test_parse_string(self) -> None:
        """Test parsing string."""
        assert _parse_value("hello") == "hello"
        assert _parse_value("hello world") == "hello world"

    def test_parse_json_list(self) -> None:
        """Test parsing JSON list."""
        assert _parse_value('["a", "b", "c"]') == ["a", "b", "c"]
        assert _parse_value("[1, 2, 3]") == [1, 2, 3]

    def test_parse_json_dict(self) -> None:
        """Test parsing JSON dict."""
        assert _parse_value('{"key": "value"}') == {"key": "value"}


class TestValueFormatting:
    """Test value formatting for display."""

    def test_format_simple_values(self) -> None:
        """Test formatting simple values."""
        assert _format_value("string") == "string"
        assert _format_value(42) == "42"
        assert _format_value(True) == "true"
        assert _format_value(False) == "false"
        assert _format_value(None) == "null"  # YAML represents None as null

    def test_format_list(self) -> None:
        """Test formatting list."""
        result = _format_value([1, 2, 3])
        assert "- 1" in result
        assert "- 2" in result
        assert "- 3" in result
        result = _format_value(["a", "b"])
        assert "- a" in result
        assert "- b" in result

    def test_format_dict(self) -> None:
        """Test formatting dict."""
        result = _format_value({"key": "value"})
        assert "key: value" in result

    def test_format_empty_collections(self) -> None:
        """Test formatting empty collections."""
        assert _format_value({}) == "{}"
        assert _format_value([]) == "[]"


@pytest.mark.asyncio
async def test_handle_command_threads_config_path_to_config_commands(tmp_path: Path) -> None:
    """`!config` dispatch should use the orchestrator-owned config file path."""
    config_path = tmp_path / "custom-config.yaml"
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=MagicMock(),
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=tmp_path),
        logger=MagicMock(),
        response_tracker=MagicMock(),
        derive_conversation_context=AsyncMock(return_value=(False, None, [])),
        requester_user_id_for_event=MagicMock(return_value="@alice:example.org"),
        resolve_reply_thread_id=MagicMock(return_value=None),
        send_response=AsyncMock(return_value=None),
        send_skill_command_response=AsyncMock(return_value=None),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        source={"content": {"body": "!config show"}},
    )
    command = Command(type=CommandType.CONFIG, args={"args_text": "show"}, raw_text="!config show")

    with patch(
        "mindroom.commands.handler.handle_config_command",
        AsyncMock(return_value=("ok", None)),
    ) as mock_handle_config_command:
        await handle_command(context=context, room=room, event=event, command=command)

    mock_handle_config_command.assert_awaited_once_with("show", runtime_paths=context.runtime_paths)


@pytest.mark.asyncio
async def test_handle_config_command_uses_explicit_runtime_paths(tmp_path: Path) -> None:
    """Direct config commands should use the provided runtime context."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"test_agent": {"display_name": "Runtime Agent", "role": "test"}},
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants_mod.set_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")

    response, change_info = await handle_config_command(
        "get agents.test_agent.display_name",
        runtime_paths=runtime_paths,
    )

    assert "Runtime Agent" in response
    assert change_info is None


@pytest.mark.asyncio
class TestConfigCommandHandling:
    """Test the config command handler."""

    async def test_handle_config_show(self) -> None:
        """Test handling config show command."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {"test_agent": {"display_name": "Test Agent", "role": "Testing"}},
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command("show", _runtime_paths_for_config(config_path))
            assert change_info is None  # show command should not return change info
            assert "Current Configuration:" in response
            assert "test_agent" in response
            assert "Test Agent" in response
        finally:
            config_path.unlink()

    async def test_handle_config_get(self) -> None:
        """Test handling config get command."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {"test_agent": {"display_name": "Test Agent", "role": "Testing"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command(
                "get agents.test_agent.display_name",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is None  # get command should not return change info
            assert "Configuration value for `agents.test_agent.display_name`:" in response
            assert "Test Agent" in response
        finally:
            config_path.unlink()

    async def test_handle_config_set(self) -> None:
        """Test handling config set command."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {"test_agent": {"display_name": "Old Name", "role": "Testing"}},
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command(
                'set agents.test_agent.display_name "New Name"',
                _runtime_paths_for_config(config_path),
            )
            assert change_info is not None  # set command should return change info for confirmation
            assert "Configuration Change Preview" in response
            assert "New Name" in response
            # Verify the change_info contains the correct values
            assert change_info["old_value"] == "Old Name"
            assert change_info["new_value"] == "New Name"
        finally:
            config_path.unlink()

    async def test_handle_config_get_nonexistent(self) -> None:
        """Test handling config get with nonexistent path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {"agents": {}}
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command(
                "get agents.nonexistent",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is None
            assert "❌" in response
            assert "not found" in response
        finally:
            config_path.unlink()

    async def test_handle_config_get_index_out_of_range(self) -> None:
        """Test handling config get with out of range array index."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {
                    "test_agent": {
                        "display_name": "Test Agent",
                        "role": "Testing",
                        "tools": ["tool1"],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command(
                "get agents.test_agent.tools.5",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is None
            assert "❌" in response
            assert "not found" in response
        finally:
            config_path.unlink()

    async def test_handle_config_set_invalid(self) -> None:
        """Test handling config set with invalid value."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "defaults": {"markdown": True},
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            # Try to set a bool field to a non-boolean string value
            response, change_info = await handle_config_command(
                "set defaults.markdown not_a_bool",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is None  # Invalid config should not return change info
            assert "❌" in response
            # The validation error should indicate the issue
        finally:
            config_path.unlink()

    async def test_handle_config_unknown_operation(self) -> None:
        """Test handling unknown config operation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({}, f)
            config_path = Path(f.name)

        try:
            response, change_info = await handle_config_command("unknown_op", _runtime_paths_for_config(config_path))
            assert change_info is None
            assert "❌ Unknown operation" in response
            assert "unknown_op" in response
        finally:
            config_path.unlink()

    async def test_handle_config_parse_error(self) -> None:
        """Test handling config command with parse error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"models": {"default": {"provider": "openai", "id": "gpt-4"}}}, f)
            config_path = Path(f.name)

        try:
            # Command with unmatched quotes
            response, change_info = await handle_config_command(
                'set test.value "unmatched',
                _runtime_paths_for_config(config_path),
            )
            assert change_info is None
            assert "❌" in response
            assert "parsing error" in response.lower()
            assert "unmatched quotes" in response.lower()
        finally:
            config_path.unlink()

    async def test_handle_config_set_unquoted_array(self) -> None:
        """Test handling config set with unquoted JSON array."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {
                    "test_agent": {
                        "display_name": "Test Agent",
                        "role": "Testing",
                        "tools": [],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            # This simulates what happens when user types: !config set path ["item1", "item2"]
            # shlex turns it into: [item1, item2] (quotes consumed)
            response, change_info = await handle_config_command(
                "set agents.test_agent.tools [communication, lobby]",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is not None  # set command should return change info
            assert "Configuration Change Preview" in response
            # Check that the change_info contains the correct new value
            assert change_info["new_value"] == ["communication", "lobby"]
        finally:
            config_path.unlink()

    async def test_handle_config_set_quoted_array(self) -> None:
        """Test handling config set with properly quoted JSON array."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "agents": {
                    "test_agent": {
                        "display_name": "Test Agent",
                        "role": "Testing",
                        "tools": [],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            # User properly quotes the entire JSON array
            response, change_info = await handle_config_command(
                'set agents.test_agent.tools ["tool1", "tool2"]',
                _runtime_paths_for_config(config_path),
            )
            assert change_info is not None  # set command should return change info
            assert "Configuration Change Preview" in response
            # Check that the change_info contains the correct new value
            assert change_info["new_value"] == ["tool1", "tool2"]
        finally:
            config_path.unlink()
