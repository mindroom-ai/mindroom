"""Tests for the namespaces a plugin manifest reserves from agent tools."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.plugins import refresh_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    publish_test_plugins,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_matrix_api_rate_limit() -> None:
    MatrixApiTools._recent_write_units.clear()


def _write_manifest(tmp_path: Path, manifest: dict[str, object]) -> Path:
    plugin_root = tmp_path / "plugins" / "example"
    plugin_root.mkdir(parents=True, exist_ok=True)
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}, plugins=["./plugins/example"]),
        test_runtime_paths(tmp_path),
    )


def _context(tmp_path: Path) -> ToolRuntimeContext:
    """Return a tool context whose turn captured a loaded plugin that reserves ``io.example.``."""
    _write_manifest(tmp_path, {"name": "example", "reserved_namespaces": ["io.example."]})
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    plugins = publish_test_plugins(config, runtime_paths)
    client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$sent:localhost", room_id="!room:localhost"),
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id="!room:localhost", thread_id=None, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        plugins=plugins,
        relations=make_relation_lookup(client=client),
        conversation_reader=make_conversation_reader_mock(),
        storage_path=tmp_path,
    )


async def _call(context: ToolRuntimeContext, **arguments: object) -> dict[str, object]:
    with patch("mindroom.custom_tools.matrix_api.logger.warning"), tool_runtime_context(context):
        return json.loads(await MatrixApiTools().matrix_api(**arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "put_state"])
@pytest.mark.parametrize(
    "content",
    [
        pytest.param({"body": "card", "io.example.card": {"title": "Approved"}}, id="top-level"),
        pytest.param(
            {
                "body": "* card",
                "m.new_content": {"body": "card", "io.example.card": {"title": "Approved"}},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$card:localhost"},
            },
            id="edit",
        ),
    ],
)
async def test_matrix_api_refuses_content_keys_a_loaded_plugin_reserves(
    tmp_path: Path,
    action: str,
    content: dict[str, object],
) -> None:
    """The model cannot forge a plugin's card from a managed account, directly or by editing a message into one."""
    context = _context(tmp_path)

    payload = await _call(context, action=action, event_type="m.room.message", content=content)

    assert payload["status"] == "error"
    assert payload["message"] == (
        "content must not set plugin-reserved keys (io.example.card); "
        "a plugin reserves their namespace for content only it writes."
    )
    context.client.room_send.assert_not_awaited()
    context.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "put_state"])
async def test_matrix_api_refuses_event_types_a_loaded_plugin_reserves(tmp_path: Path, action: str) -> None:
    """An event type in a reserved namespace is the plugin's to write, as a message or as room state."""
    context = _context(tmp_path)

    payload = await _call(context, action=action, event_type="io.example.card", content={"title": "Approved"})

    assert payload["status"] == "error"
    assert payload["message"] == "Event type 'io.example.card' is reserved by a plugin and cannot be written by a tool."
    context.client.room_send.assert_not_awaited()
    context.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_refuses_to_redact_room_state_a_loaded_plugin_reserves(tmp_path: Path) -> None:
    """Redacting a state event rewrites that state, so a plugin's reserved room state is not the model's to redact."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "content": {"mode": "strict"},
                "event_id": "$state:localhost",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 123,
                "room_id": "!room:localhost",
                "type": "io.example.settings",
                "state_key": "",
            },
        ),
    )

    payload = await _call(context, action="redact", event_id="$state:localhost")

    assert payload["status"] == "error"
    assert payload["message"] == (
        "Event type 'io.example.settings' is reserved by a plugin and cannot be written by a tool."
    )
    context.client.room_redact.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_sends_names_outside_the_reserved_namespaces(tmp_path: Path) -> None:
    """A reservation covers names under its prefix and nothing that merely starts alike."""
    context = _context(tmp_path)
    content = {"body": "card", "io.examples.card": {"title": "Approved"}}

    payload = await _call(context, action="send_event", event_type="io.examples.card", content=content)

    assert payload["status"] == "ok"
    context.client.room_send.assert_awaited_once()
    assert context.client.room_send.await_args.kwargs["content"] == content


@pytest.mark.parametrize("reserved_namespaces", ["io.example.", [1], [""], None])
def test_plugin_manifest_rejects_malformed_reserved_namespaces(tmp_path: Path, reserved_namespaces: object) -> None:
    """A manifest whose reservations are not a list of non-empty strings fails validation, naming the field."""
    _write_manifest(tmp_path, {"name": "example", "reserved_namespaces": reserved_namespaces})

    with pytest.raises(ValueError, match="Plugin reserved_namespaces must be a list of non-empty strings"):
        _config(tmp_path)


def test_refreshed_plugins_follow_an_edit_to_the_reserved_namespaces(tmp_path: Path) -> None:
    """A manifest edit that changes only the reservations reaches the plugin generation a refresh publishes."""
    manifest_path = _write_manifest(tmp_path, {"name": "example", "reserved_namespaces": ["io.example."]})
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    assert refresh_plugins(config, runtime_paths).reserved_namespaces == ("io.example.",)

    _write_manifest(tmp_path, {"name": "example", "reserved_namespaces": ["io.example.", "com.example."]})
    mtime = manifest_path.stat().st_mtime + 5
    os.utime(manifest_path, (mtime, mtime))

    assert refresh_plugins(config, runtime_paths).reserved_namespaces == ("io.example.", "com.example.")
