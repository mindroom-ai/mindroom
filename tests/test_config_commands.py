"""Tests for configuration commands."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from mindroom.commands import CommandParser, CommandType
from mindroom.config_commands import (
    format_value,
    get_nested_value,
    handle_config_command,
    parse_config_args,
    parse_value,
    set_nested_value,
)


class TestCommandParser:
    """Test config command parsing."""

    def test_parse_config_empty(self) -> None:
        """Test parsing !config with no args."""
        parser = CommandParser()
        command = parser.parse("!config")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == ""

    def test_parse_config_show(self) -> None:
        """Test parsing !config show command."""
        parser = CommandParser()
        command = parser.parse("!config show")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == "show"

    def test_parse_config_get(self) -> None:
        """Test parsing !config get command."""
        parser = CommandParser()
        command = parser.parse("!config get agents.analyst.display_name")
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == "get agents.analyst.display_name"

    def test_parse_config_set(self) -> None:
        """Test parsing !config set command."""
        parser = CommandParser()
        command = parser.parse('!config set agents.analyst.display_name "New Name"')
        assert command is not None
        assert command.type == CommandType.CONFIG
        assert command.args["args_text"] == 'set agents.analyst.display_name "New Name"'


class TestConfigArgsParsing:
    """Test config command argument parsing."""

    def test_parse_empty_args(self) -> None:
        """Test parsing empty config args defaults to show."""
        operation, args = parse_config_args("")
        assert operation == "show"
        assert args == []

    def test_parse_show_operation(self) -> None:
        """Test parsing show operation."""
        operation, args = parse_config_args("show")
        assert operation == "show"
        assert args == []

    def test_parse_get_operation(self) -> None:
        """Test parsing get operation with path."""
        operation, args = parse_config_args("get agents.analyst")
        assert operation == "get"
        assert args == ["agents.analyst"]

    def test_parse_set_operation_simple(self) -> None:
        """Test parsing set operation with simple value."""
        operation, args = parse_config_args("set defaults.markdown false")
        assert operation == "set"
        assert args == ["defaults.markdown", "false"]

    def test_parse_set_operation_quoted(self) -> None:
        """Test parsing set operation with quoted string."""
        operation, args = parse_config_args('set agents.analyst.display_name "Research Expert"')
        assert operation == "set"
        assert args == ["agents.analyst.display_name", "Research Expert"]

    def test_parse_unmatched_quotes(self) -> None:
        """Test parsing with unmatched quotes returns parse_error."""
        operation, args = parse_config_args('set test.value "unmatched')
        assert operation == "parse_error"
        assert len(args) == 1
        assert "closing quotation" in args[0].lower()

    def test_parse_mismatched_quotes(self) -> None:
        """Test parsing with mismatched quotes returns parse_error."""
        operation, args = parse_config_args("set test.value 'mismatched\"")
        assert operation == "parse_error"
        assert len(args) == 1
        assert "closing quotation" in args[0].lower()


class TestNestedValueOperations:
    """Test nested value get/set operations."""

    def test_get_nested_simple(self) -> None:
        """Test getting simple nested value."""
        data = {"agents": {"analyst": {"display_name": "Analyst"}}}
        value = get_nested_value(data, "agents.analyst.display_name")
        assert value == "Analyst"

    def test_get_nested_list(self) -> None:
        """Test getting value from list."""
        data = {"tools": ["tool1", "tool2", "tool3"]}
        value = get_nested_value(data, "tools.1")
        assert value == "tool2"

    def test_get_nested_nonexistent(self) -> None:
        """Test getting nonexistent path raises KeyError."""
        data = {"agents": {}}
        with pytest.raises(KeyError):
            get_nested_value(data, "agents.analyst.display_name")

    def test_set_nested_simple(self) -> None:
        """Test setting simple nested value."""
        data = {"agents": {"analyst": {"display_name": "Old"}}}
        set_nested_value(data, "agents.analyst.display_name", "New")
        assert data["agents"]["analyst"]["display_name"] == "New"

    def test_set_nested_create_intermediate(self) -> None:
        """Test setting creates intermediate dicts."""
        data = {"agents": {}}
        set_nested_value(data, "agents.analyst.display_name", "Analyst")
        assert data["agents"]["analyst"]["display_name"] == "Analyst"

    def test_set_nested_list(self) -> None:
        """Test setting value in list."""
        data = {"tools": ["tool1", "tool2", "tool3"]}
        set_nested_value(data, "tools.1", "new_tool")
        assert data["tools"][1] == "new_tool"


class TestValueParsing:
    """Test value parsing from strings."""

    def test_parse_boolean_true(self) -> None:
        """Test parsing true boolean."""
        assert parse_value("true") is True
        assert parse_value("True") is True

    def test_parse_boolean_false(self) -> None:
        """Test parsing false boolean."""
        assert parse_value("false") is False
        assert parse_value("False") is False

    def test_parse_none(self) -> None:
        """Test parsing None/null."""
        assert parse_value("null") is None

    def test_parse_integer(self) -> None:
        """Test parsing integer."""
        assert parse_value("42") == 42
        assert parse_value("-10") == -10

    def test_parse_float(self) -> None:
        """Test parsing float."""
        assert parse_value("3.14") == 3.14
        assert parse_value("-0.5") == -0.5

    def test_parse_string(self) -> None:
        """Test parsing string."""
        assert parse_value("hello") == "hello"
        assert parse_value("hello world") == "hello world"

    def test_parse_json_list(self) -> None:
        """Test parsing JSON list."""
        assert parse_value('["a", "b", "c"]') == ["a", "b", "c"]
        assert parse_value("[1, 2, 3]") == [1, 2, 3]

    def test_parse_json_dict(self) -> None:
        """Test parsing JSON dict."""
        assert parse_value('{"key": "value"}') == {"key": "value"}


class TestValueFormatting:
    """Test value formatting for display."""

    def test_format_simple_values(self) -> None:
        """Test formatting simple values."""
        assert format_value("string") == "string"
        assert format_value(42) == "42"
        assert format_value(True) == "true"
        assert format_value(False) == "false"
        assert format_value(None) == "null"  # YAML represents None as null

    def test_format_list(self) -> None:
        """Test formatting list."""
        result = format_value([1, 2, 3])
        assert "- 1" in result
        assert "- 2" in result
        assert "- 3" in result
        result = format_value(["a", "b"])
        assert "- a" in result
        assert "- b" in result

    def test_format_dict(self) -> None:
        """Test formatting dict."""
        result = format_value({"key": "value"})
        assert "key: value" in result

    def test_format_empty_collections(self) -> None:
        """Test formatting empty collections."""
        assert format_value({}) == "{}"
        assert format_value([]) == "[]"


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
            response, change_info = await handle_config_command("show", config_path)
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
            response, change_info = await handle_config_command("get agents.test_agent.display_name", config_path)
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
                config_path,
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
            response, change_info = await handle_config_command("get agents.nonexistent", config_path)
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
            response, change_info = await handle_config_command("get agents.test_agent.tools.5", config_path)
            assert change_info is None
            assert "❌" in response
            assert "not found" in response
        finally:
            config_path.unlink()

    async def test_handle_config_set_invalid(self) -> None:
        """Test handling config set with invalid value."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "defaults": {"num_history_runs": 5},
                "models": {"default": {"provider": "openai", "id": "gpt-4"}},
            }
            yaml.dump(config_data, f)
            config_path = Path(f.name)

        try:
            # Try to set a number field to a string value
            response, change_info = await handle_config_command(
                "set defaults.num_history_runs not_a_number",
                config_path,
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
            response, change_info = await handle_config_command("unknown_op", config_path)
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
            response, change_info = await handle_config_command('set test.value "unmatched', config_path)
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
                config_path,
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
                config_path,
            )
            assert change_info is not None  # set command should return change info
            assert "Configuration Change Preview" in response
            # Check that the change_info contains the correct new value
            assert change_info["new_value"] == ["tool1", "tool2"]
        finally:
            config_path.unlink()
