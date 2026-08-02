"""Tests for configuration commands."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import yaml

from mindroom import constants as constants_mod
from mindroom.api import config_lifecycle
from mindroom.commands import config_confirmation
from mindroom.commands.config_commands import (
    _format_value,
    _get_nested_value,
    _parse_config_args,
    _parse_value,
    _set_nested_value,
    apply_config_change,
    handle_config_command,
)
from mindroom.commands.config_confirmation import _add_confirmation_reactions, handle_confirmation_reaction
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, _CommandParser
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.delivery_gateway import SendTextRequest
from mindroom.handled_turns import TurnRecord
from mindroom.hooks import HookRegistry
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.tool_system.plugins import PluginReloadResult
from tests.conftest import make_event_cache_mock, write_config_yaml


def _runtime_paths_for_config(config_path: Path) -> constants_mod.RuntimePaths:
    return resolve_runtime_paths(config_path=config_path)


def _handler_authorization(
    *,
    config_command_enabled: bool,
    global_users: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> AuthorizationConfig:
    return AuthorizationConfig(
        config_command_enabled=config_command_enabled,
        global_users=global_users or [],
        aliases=aliases or {},
    )


def _pending_config_change(
    *,
    room_id: str = "!room:example.org",
    requester: str = "@admin:example.org",
) -> config_confirmation._PendingConfigChange:
    """Return one typed pending config change for reaction tests."""
    return config_confirmation._PendingConfigChange(
        room_id=room_id,
        thread_id=None,
        config_path="defaults.markdown",
        old_value=True,
        new_value=False,
        requester=requester,
    )


def test_authorization_config_disables_config_command_by_default() -> None:
    """Chat config commands should be disabled unless explicitly enabled."""
    assert AuthorizationConfig().config_command_enabled is False


def test_committed_config_decision_does_not_expire_with_its_preview() -> None:
    """A frozen user decision remains retry-owned beyond abandoned-preview expiry."""
    pending_change = replace(
        _pending_config_change(),
        created_at=datetime.now(UTC) - timedelta(days=2),
        decision_event_id="$reaction",
        decision_key="✅",
    )

    assert pending_change.is_expired() is False


def test_validate_and_persist_config_payload_validates_and_writes_authored_payload(tmp_path: Path) -> None:
    """Runtime config payload persistence should validate before writing."""
    config_path = tmp_path / "config.yaml"
    runtime_paths = _runtime_paths_for_config(config_path)
    config = Config(models={"default": {"provider": "openai", "id": "gpt-5.4"}})
    write_config_yaml(config, config_path)
    payload = config.authored_model_dump()
    payload["agents"] = {
        "writer": {
            "display_name": "Writer",
            "role": "Write docs",
            "rooms": ["Lab"],
        },
    }

    validated = config_lifecycle.validate_and_persist_config_payload(payload, runtime_paths)

    assert validated.agents["writer"].display_name == "Writer"
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["writer"]["rooms"] == ["Lab"]


def test_validate_and_persist_config_payload_rejects_without_overwriting(tmp_path: Path) -> None:
    """Runtime config payload persistence should leave the file untouched on validation failure."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    runtime_paths = _runtime_paths_for_config(config_path)
    config = Config(models={"default": {"provider": "openai", "id": "gpt-5.4"}})
    write_config_yaml(config, config_path)
    original_source = config_path.read_text(encoding="utf-8")
    payload = config.authored_model_dump()
    payload["plugins"] = ["./plugins/bad-name"]

    with pytest.raises(ConfigRuntimeValidationError):
        config_lifecycle.validate_and_persist_config_payload(payload, runtime_paths)

    assert config_path.read_text(encoding="utf-8") == original_source


@pytest.mark.asyncio
async def test_add_confirmation_reactions_sends_confirm_and_cancel_annotations() -> None:
    """Config confirmation should add canonical Matrix annotation reactions."""
    client = AsyncMock()
    response = MagicMock(spec=nio.RoomSendResponse)
    client.room_send.return_value = response
    await _add_confirmation_reactions(client, "!room:example.org", "$preview")

    assert [call.kwargs["content"] for call in client.room_send.await_args_list] == [
        {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$preview",
                "key": "✅",
            },
        },
        {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$preview",
                "key": "❌",
            },
        },
    ]
    # Reactions must reach encrypted rooms; bots deliver to unverified devices.
    assert all(call.kwargs["ignore_unverified_devices"] is True for call in client.room_send.await_args_list)
    transaction_ids = [call.kwargs["tx_id"] for call in client.room_send.await_args_list]
    assert len(set(transaction_ids)) == 2
    assert all(transaction_id.startswith("mindroom-config-reaction-") for transaction_id in transaction_ids)


@pytest.mark.asyncio
async def test_confirmation_setup_errors_remain_retryable() -> None:
    """Matrix state and reaction failures must propagate to the command owner."""
    pending_change = _pending_config_change()
    state_client = AsyncMock()
    state_client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    with pytest.raises(RuntimeError, match="Failed to store pending config change"):
        await config_confirmation._store_pending_change_in_matrix(state_client, "$preview", pending_change)

    reaction_client = AsyncMock()
    reaction_client.room_send.return_value = nio.RoomSendError.from_dict(
        {"errcode": "M_LIMIT_EXCEEDED", "error": "Slow down"},
        "!room:example.org",
    )
    with pytest.raises(RuntimeError, match="Failed to add confirm"):
        await _add_confirmation_reactions(reaction_client, "!room:example.org", "$preview")


@pytest.mark.asyncio
async def test_confirmation_setup_adopts_existing_duplicate_reaction() -> None:
    """A replayed setup should accept the homeserver's duplicate-annotation proof."""
    client = AsyncMock()
    client.room_send.side_effect = [
        nio.RoomSendError("already exists", "M_DUPLICATE_ANNOTATION"),
        nio.RoomSendResponse("$cancel-reaction", "!room:example.org"),
    ]

    await _add_confirmation_reactions(client, "!room:example.org", "$preview")

    assert client.room_send.await_count == 2


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
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@alice:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$response"),
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
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id="@alice:example.org",
        )

    mock_handle_config_command.assert_awaited_once_with("show", runtime_paths=context.runtime_paths)


@pytest.mark.asyncio
async def test_handle_command_config_disabled_by_default(tmp_path: Path) -> None:
    """Disabled config commands should reject before loading or previewing config."""
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=False,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@admin:example.org",
        event_id="$event",
        source={"content": {"body": "!config show"}},
    )
    command = Command(type=CommandType.CONFIG, args={"args_text": "show"}, raw_text="!config show")

    with patch(
        "mindroom.commands.handler.handle_config_command",
        AsyncMock(return_value=("ok", None)),
    ) as mock_handle_config_command:
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id="@admin:example.org",
        )

    mock_handle_config_command.assert_not_awaited()
    assert context.send_response.await_args.args[0] == "❌ Config command disabled."


@pytest.mark.asyncio
async def test_handle_command_config_enabled_requires_admin(tmp_path: Path) -> None:
    """Enabled config commands should still require a global admin."""
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
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
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id="@alice:example.org",
        )

    mock_handle_config_command.assert_not_awaited()
    assert context.send_response.await_args.args[0] == "❌ Admin only."


@pytest.mark.asyncio
async def test_handle_command_records_response_event_id_for_standard_reply(tmp_path: Path) -> None:
    """Standard command replies should record the emitted Matrix response event ID."""
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=MagicMock(),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        source={"content": {"body": "!help"}},
    )
    command = Command(type=CommandType.HELP, args={"topic": None}, raw_text="!help")

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@alice:example.org",
    )

    context.record_handled_turn.assert_called_once_with(
        TurnRecord.create(
            ["$event"],
            response_event_id="$reply",
        ),
    )


@pytest.mark.asyncio
async def test_handle_command_reload_plugins_requires_admin_and_uses_callback(tmp_path: Path) -> None:
    """Reload-plugins should be admin-only and call the injected reload callback once."""
    command = Command(type=CommandType.RELOAD_PLUGINS, args={}, raw_text="!reload-plugins")
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@admin:example.org",
        event_id="$event",
        source={"content": {"body": "!reload-plugins"}},
    )
    reload_plugins = AsyncMock(
        return_value=PluginReloadResult(HookRegistry.empty(), ("demo-plugin",), 1),
    )

    admin_context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(authorization=AuthorizationConfig(global_users=["@admin:example.org"])),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        reload_plugins=reload_plugins,
    )
    await handle_command(
        context=admin_context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@admin:example.org",
    )

    reload_plugins.assert_awaited_once()
    assert "demo-plugin" in admin_context.send_response.await_args.args[0]

    user_context = CommandHandlerContext(
        **{**admin_context.__dict__, "config": SimpleNamespace(authorization=AuthorizationConfig(global_users=[]))},
    )
    await handle_command(
        context=user_context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@user:example.org",
    )

    reload_plugins.assert_awaited_once()
    assert user_context.send_response.await_args.args[0] == "❌ Admin only."


@pytest.mark.asyncio
async def test_handle_command_reload_plugins_allows_alias_mapped_admin(tmp_path: Path) -> None:
    """Reload-plugins should treat alias-backed global admins as admins."""
    command = Command(type=CommandType.RELOAD_PLUGINS, args={}, raw_text="!reload-plugins")
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@telegram_admin:example.org",
        event_id="$event",
        source={"content": {"body": "!reload-plugins"}},
    )
    reload_plugins = AsyncMock(
        return_value=PluginReloadResult(HookRegistry.empty(), ("demo-plugin",), 0),
    )
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(
            authorization=AuthorizationConfig(
                global_users=["@admin:example.org"],
                aliases={"@admin:example.org": ["@telegram_admin:example.org"]},
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        reload_plugins=reload_plugins,
    )

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@telegram_admin:example.org",
    )

    reload_plugins.assert_awaited_once()
    assert context.send_response.await_args.args[0] == "✅ Reloaded 1 plugin; cancelled 0 tasks; active: demo-plugin"


@pytest.mark.asyncio
async def test_handle_command_reload_plugins_surfaces_reload_failure(tmp_path: Path) -> None:
    """Reload-plugins should report reload failures instead of a success summary."""
    command = Command(type=CommandType.RELOAD_PLUGINS, args={}, raw_text="!reload-plugins")
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@admin:example.org",
        event_id="$event",
        source={"content": {"body": "!reload-plugins"}},
    )
    reload_plugins = AsyncMock(side_effect=RuntimeError("Plugin hooks module not found: /tmp/demo/hooks.py"))
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(authorization=AuthorizationConfig(global_users=["@admin:example.org"])),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        reload_plugins=reload_plugins,
    )

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@admin:example.org",
    )

    assert (
        context.send_response.await_args.args[0]
        == "❌ Plugin reload failed: Plugin hooks module not found: /tmp/demo/hooks.py"
    )


@pytest.mark.asyncio
async def test_handle_command_config_set_confirmation_records_preview_event_id(tmp_path: Path) -> None:
    """Config preview replies should persist confirmation state and record the preview event ID."""
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@alice:example.org"],
                aliases={"@alice:example.org": ["@telegram_alice:example.org"]},
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$preview"),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@telegram_alice:example.org",
        event_id="$event",
        source={"content": {"body": "!config set defaults.markdown false"}},
    )
    command = Command(
        type=CommandType.CONFIG,
        args={"args_text": "set defaults.markdown false"},
        raw_text="!config set defaults.markdown false",
    )
    change_info = {
        "config_path": "defaults.markdown",
        "old_value": True,
        "new_value": False,
    }
    with (
        patch(
            "mindroom.commands.handler.handle_config_command",
            AsyncMock(return_value=("preview", change_info)),
        ),
        patch(
            "mindroom.commands.handler.config_confirmation.ensure_pending_change",
            new_callable=AsyncMock,
        ) as mock_ensure_pending,
    ):
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id="@telegram_alice:example.org",
        )

    mock_ensure_pending.assert_awaited_once_with(
        context.client,
        event_id="$preview",
        room_id="!room:example.org",
        thread_id=None,
        config_path="defaults.markdown",
        old_value=True,
        new_value=False,
        requester="@alice:example.org",
    )
    context.record_handled_turn.assert_called_once_with(
        TurnRecord.create(
            ["$event"],
            response_event_id="$preview",
        ),
    )


@pytest.mark.asyncio
async def test_handle_command_config_set_stays_retryable_after_post_send_failure(tmp_path: Path) -> None:
    """Confirmation setup failures should leave the command retryable."""
    context = CommandHandlerContext(
        client=AsyncMock(),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@alice:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve("!room:example.org", None, "$event"),
        record_handled_turn=MagicMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$preview"),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        source={"content": {"body": "!config set defaults.markdown false"}},
    )
    command = Command(
        type=CommandType.CONFIG,
        args={"args_text": "set defaults.markdown false"},
        raw_text="!config set defaults.markdown false",
    )
    change_info = {
        "config_path": "defaults.markdown",
        "old_value": True,
        "new_value": False,
    }

    with (
        patch(
            "mindroom.commands.handler.handle_config_command",
            AsyncMock(return_value=("preview", change_info)),
        ),
        patch(
            "mindroom.commands.handler.config_confirmation.ensure_pending_change",
            new_callable=AsyncMock,
            side_effect=RuntimeError("reaction failure"),
        ),
        pytest.raises(RuntimeError, match="reaction failure"),
    ):
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id="@alice:example.org",
        )

    context.record_handled_turn.assert_not_called()


@pytest.mark.asyncio
async def test_handle_confirmation_reaction_respects_disabled_config_command(tmp_path: Path) -> None:
    """Disabling !config should also block already-pending confirmation reactions."""
    target = MessageTarget.resolve("!room:example.org", None, "$preview")
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=False,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(return_value=target),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$event")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(event_id="$reaction", sender="@admin:example.org", key="✅", reacts_to="$preview")
    pending_change = _pending_config_change()

    with (
        patch.dict(config_confirmation._pending_changes, {"$preview": pending_change}, clear=True),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch("mindroom.commands.config_commands.apply_config_change", new_callable=AsyncMock) as mock_apply,
    ):
        await handle_confirmation_reaction(bot, room, event)

    mock_apply.assert_not_awaited()
    bot._delivery_gateway.send_text.assert_awaited_once_with(
        SendTextRequest(
            target=target,
            response_text="❌ Config command disabled.",
            skip_mentions=True,
            extra_content={constants_mod.CONFIG_CONFIRMATION_REACTION_KEY: "$reaction"},
        ),
    )


@pytest.mark.asyncio
async def test_handle_confirmation_reaction_requires_current_admin(tmp_path: Path) -> None:
    """Confirmation should fail if hot reload removed the requester from global admins."""
    target = MessageTarget.resolve("!room:example.org", None, "$preview")
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@other-admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(return_value=target),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$event")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(event_id="$reaction", sender="@admin:example.org", key="✅", reacts_to="$preview")
    pending_change = _pending_config_change()

    with (
        patch.dict(config_confirmation._pending_changes, {"$preview": pending_change}, clear=True),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch("mindroom.commands.config_commands.apply_config_change", new_callable=AsyncMock) as mock_apply,
    ):
        await handle_confirmation_reaction(bot, room, event)

    mock_apply.assert_not_awaited()
    bot._delivery_gateway.send_text.assert_awaited_once_with(
        SendTextRequest(
            target=target,
            response_text="❌ Admin only.",
            skip_mentions=True,
            extra_content={constants_mod.CONFIG_CONFIRMATION_REACTION_KEY: "$reaction"},
        ),
    )


@pytest.mark.asyncio
async def test_handle_confirmation_reaction_accepts_alias_backed_requester(tmp_path: Path) -> None:
    """Alias-backed admins should be able to confirm their own pending config changes."""
    target = MessageTarget.resolve("!room:example.org", None, "$preview")
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@admin:example.org"],
                aliases={"@admin:example.org": ["@telegram_admin:example.org"]},
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(return_value=target),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$event")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        event_id="$reaction",
        sender="@telegram_admin:example.org",
        key="✅",
        reacts_to="$preview",
    )
    pending_change = _pending_config_change()

    with (
        patch.dict(config_confirmation._pending_changes, {"$preview": pending_change}, clear=True),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch(
            "mindroom.commands.config_commands.apply_config_change",
            new_callable=AsyncMock,
            return_value="✅ Configuration updated successfully.",
        ) as mock_apply,
    ):
        await handle_confirmation_reaction(bot, room, event)

    mock_apply.assert_awaited_once_with(
        "defaults.markdown",
        False,
        runtime_paths=bot.runtime_paths,
    )
    bot._delivery_gateway.send_text.assert_awaited_once_with(
        SendTextRequest(
            target=target,
            response_text="✅ Configuration updated successfully.",
            skip_mentions=True,
            extra_content={constants_mod.CONFIG_CONFIRMATION_REACTION_KEY: "$reaction"},
        ),
    )


@pytest.mark.asyncio
async def test_confirmation_reactions_serialize_one_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Competing confirm/cancel reactions must produce one side effect and response."""
    preview_event_id = "$preview"
    pending_change = _pending_config_change()
    monkeypatch.setattr(config_confirmation, "_pending_changes", {preview_event_id: pending_change})
    monkeypatch.setattr(config_confirmation, "_pending_change_locks", {})
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(return_value=MessageTarget.resolve("!room:example.org", None, "$preview")),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$decision-response")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    confirm = SimpleNamespace(
        event_id="$confirm",
        sender="@admin:example.org",
        key="✅",
        reacts_to=preview_event_id,
    )
    cancel = SimpleNamespace(
        event_id="$cancel",
        sender="@admin:example.org",
        key="❌",
        reacts_to=preview_event_id,
    )

    async def current_pending(*_args: object) -> config_confirmation._PendingConfigChange | None:
        return config_confirmation._get_pending_change(preview_event_id)

    with (
        patch(
            "mindroom.commands.config_confirmation._resolve_pending_change",
            new=AsyncMock(side_effect=current_pending),
        ),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch(
            "mindroom.commands.config_commands.apply_config_change",
            new_callable=AsyncMock,
            return_value="✅ Configuration updated successfully.",
        ) as apply_change,
    ):
        await asyncio.gather(
            handle_confirmation_reaction(bot, room, confirm),
            handle_confirmation_reaction(bot, room, cancel),
        )

    apply_change.assert_awaited_once()
    bot._delivery_gateway.send_text.assert_awaited_once()
    assert config_confirmation._get_pending_change(preview_event_id) is None
    assert not config_confirmation._pending_change_locks


@pytest.mark.asyncio
async def test_checkpointed_confirmation_ignores_changed_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay must honor the authorization frozen before an applied config change."""
    preview_event_id = "$preview"
    reaction_event_id = "$reaction"
    pending_change = replace(
        _pending_config_change(),
        decision_event_id=reaction_event_id,
        decision_key="✅",
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {preview_event_id: pending_change})
    monkeypatch.setattr(config_confirmation, "_pending_change_locks", {})
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=False,
                global_users=[],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(
                return_value=MessageTarget.resolve("!room:example.org", None, preview_event_id),
            ),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$response")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        event_id=reaction_event_id,
        sender="@admin:example.org",
        key="✅",
        reacts_to=preview_event_id,
    )

    with (
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch(
            "mindroom.commands.config_commands.apply_config_change",
            new_callable=AsyncMock,
            return_value="✅ Configuration updated successfully.",
        ) as apply_change,
    ):
        await handle_confirmation_reaction(bot, room, event)

    apply_change.assert_awaited_once()
    request = bot._delivery_gateway.send_text.await_args.args[0]
    assert request.response_text == "✅ Configuration updated successfully."


@pytest.mark.asyncio
async def test_resolve_pending_change_loads_exact_matrix_state_before_room_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaction recovery must not depend on asynchronous room-startup restoration."""
    event_id = "$preview"
    room_id = "!room:example.org"
    pending_change = config_confirmation._PendingConfigChange(
        room_id=room_id,
        thread_id="$thread",
        config_path="defaults.markdown",
        old_value=True,
        new_value=False,
        requester="@admin:example.org",
    )
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content=pending_change.to_dict(),
        event_type=config_confirmation._PENDING_CONFIG_EVENT_TYPE,
        state_key=event_id,
        room_id=room_id,
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {})

    resolved = await config_confirmation._resolve_pending_change(client, room_id, event_id)

    assert resolved == pending_change
    assert config_confirmation._get_pending_change(event_id) == pending_change
    client.room_get_state_event.assert_awaited_once_with(
        room_id,
        config_confirmation._PENDING_CONFIG_EVENT_TYPE,
        event_id,
    )


@pytest.mark.asyncio
async def test_confirmation_send_failure_keeps_replay_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed config side effect stays retryable until its response is visible."""
    event_id = "$preview"
    room_id = "!room:example.org"
    target = MessageTarget.resolve(room_id, None, event_id)
    pending_change = config_confirmation._PendingConfigChange(
        room_id=room_id,
        thread_id=None,
        config_path="defaults.markdown",
        old_value=True,
        new_value=False,
        requester="@admin:example.org",
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {event_id: pending_change})
    monkeypatch.setattr(config_confirmation, "_pending_change_locks", {})
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(build_message_target=MagicMock(return_value=target)),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value=None)),
    )
    room = SimpleNamespace(room_id=room_id)
    event = SimpleNamespace(event_id="$reaction", sender="@admin:example.org", key="✅", reacts_to=event_id)

    with (
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ) as remove_matrix,
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_commands.apply_config_change",
            new_callable=AsyncMock,
            return_value="✅ Configuration updated successfully.",
        ) as apply_change,
    ):
        with pytest.raises(RuntimeError, match="Failed to send config confirmation response"):
            await handle_confirmation_reaction(bot, room, event)
        assert not config_confirmation._pending_change_locks

        bot._delivery_gateway.send_text = AsyncMock(return_value="$response")
        await handle_confirmation_reaction(bot, room, event)

    apply_change.assert_awaited_once()
    remove_matrix.assert_awaited_once_with(bot.client, room_id, event_id)
    assert config_confirmation._get_pending_change(event_id) is None
    assert not config_confirmation._pending_change_locks


@pytest.mark.asyncio
async def test_ambiguous_config_execution_reports_uncertainty_without_reapplying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the config-write boundary must not overwrite newer state."""
    preview_event_id = "$preview"
    reaction_event_id = "$reaction"
    pending_change = replace(
        _pending_config_change(),
        decision_event_id=reaction_event_id,
        decision_key="✅",
        decision_execution_started=True,
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {preview_event_id: pending_change})
    monkeypatch.setattr(config_confirmation, "_pending_change_locks", {})
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(authorization=_handler_authorization(config_command_enabled=True)),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(
            build_message_target=MagicMock(
                return_value=MessageTarget.resolve("!room:example.org", None, preview_event_id),
            ),
        ),
        _delivery_gateway=MagicMock(send_text=AsyncMock(return_value="$response")),
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        event_id=reaction_event_id,
        sender="@admin:example.org",
        key="✅",
        reacts_to=preview_event_id,
    )

    with (
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset(),
        ),
        patch(
            "mindroom.commands.config_confirmation._store_pending_change_in_matrix",
            new_callable=AsyncMock,
        ),
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ),
        patch("mindroom.commands.config_commands.apply_config_change", new_callable=AsyncMock) as apply_change,
    ):
        await handle_confirmation_reaction(bot, room, event)

    apply_change.assert_not_awaited()
    request = bot._delivery_gateway.send_text.await_args.args[0]
    assert "outcome is uncertain" in request.response_text


@pytest.mark.asyncio
async def test_config_preview_recovery_preserves_committed_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command replay must adopt committed confirmation state without reopening it."""
    preview_event_id = "$preview"
    pending_change = replace(
        _pending_config_change(),
        decision_event_id="$reaction",
        decision_key="✅",
        decision_execution_started=True,
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {preview_event_id: pending_change})
    monkeypatch.setattr(config_confirmation, "_pending_change_locks", {})
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@router:example.org"

    with patch(
        "mindroom.commands.config_confirmation._add_confirmation_reactions",
        new_callable=AsyncMock,
    ) as add_reactions:
        recovered = await config_confirmation.recover_confirmation_setup(
            client,
            pending_change.room_id,
            preview_event_id,
        )
        await config_confirmation.ensure_pending_change(
            client,
            event_id=preview_event_id,
            room_id=pending_change.room_id,
            thread_id=None,
            config_path="defaults.markdown",
            old_value=False,
            new_value=True,
            requester="@other:example.org",
        )

    assert recovered is True
    assert config_confirmation._get_pending_change(preview_event_id) == pending_change
    add_reactions.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_recovery_adopts_untracked_visible_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay after send must settle state without applying or responding twice."""
    event_id = "$preview"
    room_id = "!room:example.org"
    pending_change = config_confirmation._PendingConfigChange(
        room_id=room_id,
        thread_id=None,
        config_path="defaults.markdown",
        old_value=True,
        new_value=False,
        requester="@admin:example.org",
    )
    monkeypatch.setattr(config_confirmation, "_pending_changes", {event_id: pending_change})
    bot = SimpleNamespace(
        client=SimpleNamespace(user_id="@router:example.org"),
        config=SimpleNamespace(
            authorization=_handler_authorization(
                config_command_enabled=True,
                global_users=["@admin:example.org"],
            ),
        ),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        _conversation_resolver=SimpleNamespace(build_message_target=MagicMock()),
        _delivery_gateway=MagicMock(send_text=AsyncMock()),
    )
    room = SimpleNamespace(room_id=room_id)
    event = SimpleNamespace(event_id="$reaction", sender="@admin:example.org", key="✅", reacts_to=event_id)

    with (
        patch(
            "mindroom.commands.config_confirmation.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset({"$response"}),
        ) as find_response,
        patch(
            "mindroom.commands.config_confirmation._remove_pending_change_from_matrix",
            new_callable=AsyncMock,
        ) as remove_matrix,
        patch("mindroom.commands.config_commands.apply_config_change", new_callable=AsyncMock) as apply_change,
    ):
        await handle_confirmation_reaction(bot, room, event)

    apply_change.assert_not_awaited()
    bot._delivery_gateway.send_text.assert_not_awaited()
    remove_matrix.assert_awaited_once_with(bot.client, room_id, event_id)
    assert config_confirmation._get_pending_change(event_id) is None
    response_filter = find_response.await_args.kwargs["response_source_filter"]
    assert response_filter(
        {"content": {constants_mod.CONFIG_CONFIRMATION_REACTION_KEY: "$reaction"}},
    )
    assert not response_filter(
        {"content": {constants_mod.CONFIG_CONFIRMATION_REACTION_KEY: "$different"}},
    )


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
    runtime_paths = constants_mod.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
    )

    response, change_info = await handle_config_command(
        "get agents.test_agent.display_name",
        runtime_paths=runtime_paths,
    )

    assert "Runtime Agent" in response
    assert change_info is None


@pytest.mark.asyncio
async def test_handle_config_command_rejects_runtime_sensitive_invalid_change(tmp_path: Path) -> None:
    """Config previews should validate against the explicit runtime context."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=config_path,
        process_env={"MINDROOM_NAMESPACE": "prod1"},
    )
    matrix_state = MatrixState.load(runtime_paths=runtime_paths)
    matrix_state.add_account("agent_assistant", "mindroom_assistant_prod1", "pw", domain="localhost")
    matrix_state.save(runtime_paths=runtime_paths)

    response, change_info = await handle_config_command(
        "set mindroom_user.username mindroom_assistant_prod1",
        runtime_paths=runtime_paths,
    )

    assert "Invalid configuration" in response
    assert "mindroom_user.username" in response
    assert change_info is None


@pytest.mark.asyncio
async def test_handle_config_command_show_tolerates_invalid_plugin_manifest(tmp_path: Path) -> None:
    """Show should keep working when runtime plugin loading degrades."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/bad-name"],
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command("show", _runtime_paths_for_config(config_path))

    assert change_info is None
    assert "Current Configuration:" in response
    assert "assistant" in response
    assert "./plugins/bad-name" in response
    assert "Invalid configuration" not in response


@pytest.mark.asyncio
async def test_handle_config_command_show_redacts_secrets(tmp_path: Path) -> None:
    """Config show output should not expose inline provider credentials."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "default": {
                        "provider": "openai",
                        "id": "gpt-5.4",
                        "api_key": "sk-test-config-secret",
                    },
                },
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command("show", _runtime_paths_for_config(config_path))

    assert change_info is None
    assert "api_key:" in response
    assert "***redacted***" in response
    assert "sk-test-config-secret" not in response


@pytest.mark.asyncio
async def test_handle_config_command_get_redacts_secret_values(tmp_path: Path) -> None:
    """Config get output should redact a sensitive leaf value."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "default": {
                        "provider": "openai",
                        "id": "gpt-5.4",
                        "api_key": "sk-test-config-secret",
                    },
                },
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command(
        "get models.default.api_key",
        _runtime_paths_for_config(config_path),
    )

    assert change_info is None
    assert "Configuration value for `models.default.api_key`:" in response
    assert "***redacted***" in response
    assert "sk-test-config-secret" not in response


@pytest.mark.asyncio
async def test_handle_config_command_set_preview_redacts_secret_values(tmp_path: Path) -> None:
    """Config set preview should redact old and new sensitive leaf values."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "default": {
                        "provider": "openai",
                        "id": "gpt-5.4",
                        "api_key": "sk-old-config-secret",
                    },
                },
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command(
        "set models.default.api_key sk-new-config-secret",
        _runtime_paths_for_config(config_path),
    )

    assert change_info is not None
    assert "Configuration Change Preview" in response
    assert "***redacted***" in response
    assert "sk-old-config-secret" not in response
    assert "sk-new-config-secret" not in response
    assert change_info["old_value"] == "sk-old-config-secret"
    assert change_info["new_value"] == "sk-new-config-secret"


@pytest.mark.asyncio
async def test_handle_config_command_show_returns_malformed_yaml_error(tmp_path: Path) -> None:
    """Show should return a user-facing error when the config YAML is malformed."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text("agents:\n  bad: [\n", encoding="utf-8")

    response, change_info = await handle_config_command("show", _runtime_paths_for_config(config_path))

    assert change_info is None
    assert "Invalid configuration" in response
    assert "Could not parse configuration YAML" in response
    assert "Changes were NOT applied." not in response


@pytest.mark.asyncio
async def test_handle_config_command_set_returns_invalid_plugin_manifest_error(tmp_path: Path) -> None:
    """Set previews should surface plugin manifest validation failures as user errors."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": [],
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command(
        'set plugins ["./plugins/bad-name"]',
        _runtime_paths_for_config(config_path),
    )

    assert change_info is None
    assert "Invalid configuration" in response
    assert "Invalid plugin name" in response


@pytest.mark.asyncio
async def test_handle_config_command_set_returns_malformed_plugin_manifest_error(tmp_path: Path) -> None:
    """Set previews should surface malformed plugin manifests as user errors."""
    plugin_root = tmp_path / "plugins" / "bad-manifest"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good_plugin", "tools_module": 123, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": [],
            },
        ),
        encoding="utf-8",
    )

    response, change_info = await handle_config_command(
        'set plugins ["./plugins/bad-manifest"]',
        _runtime_paths_for_config(config_path),
    )

    assert change_info is None
    assert "Invalid configuration" in response
    assert "Plugin tools_module must be a string" in response


@pytest.mark.asyncio
async def test_apply_config_change_returns_invalid_plugin_manifest_error(tmp_path: Path) -> None:
    """Confirmed config apply should keep runtime validation in the invalid-config channel."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/bad-name"],
            },
        ),
        encoding="utf-8",
    )

    response = await apply_config_change(
        "defaults.markdown",
        False,
        _runtime_paths_for_config(config_path),
    )

    assert "Invalid configuration" in response
    assert "Invalid plugin name" in response


@pytest.mark.asyncio
async def test_apply_config_change_preserves_call_profile_authorship(tmp_path: Path) -> None:
    """Confirmed edits preserve complete profiles and agent assignments."""
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "inherited": {"display_name": "Inherited"},
                    "cleared": {"display_name": "Cleared"},
                },
                "calls": {
                    "enabled": True,
                    "profiles": {
                        "openai-realtime": {
                            "backend": "realtime",
                            "model": "gpt-realtime-2.1",
                            "credentials_service": "openai-voice",
                            "voice": "marin",
                        },
                    },
                    "agents": {
                        "inherited": "openai-realtime",
                        "cleared": "openai-realtime",
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    response = await apply_config_change(
        "defaults.markdown",
        False,
        _runtime_paths_for_config(config_path),
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "Configuration updated successfully" in response
    assert saved["calls"]["profiles"] == {
        "openai-realtime": {
            "backend": "realtime",
            "model": "gpt-realtime-2.1",
            "credentials_service": "openai-voice",
            "voice": "marin",
        },
    }
    assert saved["calls"]["agents"] == {
        "cleared": "openai-realtime",
        "inherited": "openai-realtime",
    }


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
                        "tools": ["shell"],
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
                "set agents.test_agent.tools [matrix_message, scheduler]",
                _runtime_paths_for_config(config_path),
            )
            assert change_info is not None  # set command should return change info
            assert "Configuration Change Preview" in response
            # Check that the change_info contains the correct new value
            assert change_info["new_value"] == ["matrix_message", "scheduler"]
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
                'set agents.test_agent.tools ["shell", "coding"]',
                _runtime_paths_for_config(config_path),
            )
            assert change_info is not None  # set command should return change info
            assert "Configuration Change Preview" in response
            # Check that the change_info contains the correct new value
            assert change_info["new_value"] == ["shell", "coding"]
        finally:
            config_path.unlink()
