"""Tests for a room's `thread_mode` setting, the override an older runtime stored for it, and `!thread_mode`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, command_parser, get_command_help
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths, tracking_dir
from mindroom.message_target import MessageTarget
from mindroom.room_settings import THREAD_MODE, resolve_room_setting
from tests.authorization_helpers import (
    make_test_command_handler_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.room_settings_helpers import RoomStateHomeserver, set_room_thread_mode_override

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
SETTING = ("io.mindroom.room_settings", "thread_mode")
resolve_room_thread_mode_override = partial(resolve_room_setting, THREAD_MODE)


def _store_path(runtime_paths: RuntimePaths) -> Path:
    """The file an older runtime stored room thread mode overrides in."""
    return tracking_dir(runtime_paths) / "room_thread_modes.json"


@dataclass(frozen=True)
class _ThreadModeEvent:
    sender: str
    event_id: str
    body: str
    source: dict[str, dict[str, str]]


def _runtime_bound_config(config: Config, tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(config, runtime_paths)


def _homeserver(admin: str = "@admin:localhost") -> RoomStateHomeserver:
    """A room whose only admin is ``admin``."""
    homeserver = RoomStateHomeserver()
    homeserver.events["m.room.power_levels", ""] = {"users": {admin: 100}, "users_default": 0}
    return homeserver


def _thread_mode_context(tmp_path: Path, client: AsyncMock) -> CommandHandlerContext:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return make_test_command_handler_context(
        client=client,
        config=Config(),
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data"),
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )


@pytest.fixture
def thread_mode_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the room take the thread mode state, which these tests do not look at."""
    monkeypatch.setattr(
        "mindroom.commands.thread_mode_commands.reconcile_room_thread_mode",
        AsyncMock(return_value=True),
    )


def _thread_mode_event(sender: str, body: str) -> _ThreadModeEvent:
    return _ThreadModeEvent(
        sender=sender,
        event_id="$event",
        body=body,
        source={"content": {"body": body}},
    )


def test_room_thread_mode_setting_is_scoped_by_room(tmp_path: Path) -> None:
    """A room's thread mode setting applies to that room alone."""
    runtime_paths = test_runtime_paths(tmp_path)

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) is None
    set_room_thread_mode_override(runtime_paths, room_id=ROOM_ID, mode="room")

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) == "room"
    assert resolve_room_thread_mode_override(runtime_paths, "!other:localhost") is None
    assert resolve_room_thread_mode_override(runtime_paths, None) is None


def test_an_older_runtimes_thread_mode_file_reads_as_empty_when_corrupt(tmp_path: Path) -> None:
    """A corrupt file an older runtime left reads as no override."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) is None


def test_an_older_runtimes_thread_mode_file_ignores_invalid_records(tmp_path: Path) -> None:
    """Records an older runtime stored with no thread mode are ignored."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                ROOM_ID: {"mode": "invalid", "set_at": "2026-06-17T00:00:00+00:00"},
                "!container:localhost": {"mode": [], "set_at": "2026-06-17T00:00:00+00:00"},
            },
        ),
        encoding="utf-8",
    )

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) is None
    assert resolve_room_thread_mode_override(runtime_paths, "!container:localhost") is None


def test_an_older_runtimes_thread_mode_file_is_found_after_a_missing_read(tmp_path: Path) -> None:
    """A missing-file read should not hide a file found later."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) is None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({ROOM_ID: {"mode": "thread", "set_at": "2026-06-17T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    assert resolve_room_thread_mode_override(runtime_paths, ROOM_ID) == "thread"


def test_get_entity_thread_mode_prefers_runtime_room_override(tmp_path: Path) -> None:
    """Runtime room overrides should beat static config for agents, teams, and router."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    rooms=[ROOM_ID],
                    thread_mode="thread",
                    room_thread_modes={ROOM_ID: "thread"},
                ),
                "coder": AgentConfig(
                    display_name="Coder",
                    rooms=[ROOM_ID],
                    thread_mode="thread",
                ),
            },
            teams={
                "ops": TeamConfig(
                    display_name="Ops",
                    role="Operations",
                    agents=["assistant", "coder"],
                    rooms=[ROOM_ID],
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)

    set_room_thread_mode_override(
        runtime_paths,
        room_id=ROOM_ID,
        mode="room",
    )

    assert config.get_entity_thread_mode("assistant", runtime_paths, room_id=ROOM_ID) == "room"
    assert config.get_entity_thread_mode("ops", runtime_paths, room_id=ROOM_ID) == "room"
    assert config.get_entity_thread_mode(ROUTER_AGENT_NAME, runtime_paths, room_id=ROOM_ID) == "room"
    assert config.get_entity_thread_mode("assistant", runtime_paths, room_id="!other:localhost") == "thread"


def test_thread_mode_command_parsing() -> None:
    """The parser should recognize !thread_mode aliases with and without arguments."""
    command = command_parser.parse("!thread_mode")
    assert command is not None
    assert command.type == CommandType.THREAD_MODE
    assert command.args["args_text"] == ""

    command = command_parser.parse("!thread-mode room")
    assert command is not None
    assert command.type == CommandType.THREAD_MODE
    assert command.args["args_text"] == "room"

    command = command_parser.parse("!threadmode reset")
    assert command is not None
    assert command.type == CommandType.THREAD_MODE
    assert command.args["args_text"] == "reset"

    command = command_parser.parse("!thread_mode thread")
    assert command is not None
    assert command.type == CommandType.THREAD_MODE
    assert command.args["args_text"] == "thread"

    command = command_parser.parse("!thread_mode show")
    assert command is not None
    assert command.type == CommandType.THREAD_MODE
    assert command.args["args_text"] == "show"


def test_thread_mode_help_accepts_command_aliases() -> None:
    """Help should show the thread-mode topic for every command spelling."""
    for topic in ("thread_mode", "thread-mode", "threadmode"):
        assert "**Thread Mode Command**" in get_command_help(topic)


@pytest.mark.asyncio
async def test_thread_mode_show_reports_the_room_override(tmp_path: Path) -> None:
    """`!thread_mode show` names the room's override, or says configured modes apply without one."""
    homeserver = _homeserver()
    context = _thread_mode_context(tmp_path, homeserver.client())
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "show"}, raw_text="!thread_mode show")

    async def show() -> str:
        await handle_command(
            context=context,
            room=SimpleNamespace(room_id=ROOM_ID),
            event=_thread_mode_event("@user:localhost", "!thread_mode show"),
            command=command,
            requester_user_id="@user:localhost",
        )
        return context.send_response.await_args.args[0]

    assert (await show()).startswith("No room thread mode override is set.")
    # A client's change the runtime's copy has not seen: the command reads the room itself.
    homeserver.events[SETTING] = {"value": "room"}
    assert (await show()).startswith("This room uses the `room` thread mode override.")
    context.client.room_get_state_event.side_effect = None
    context.client.room_get_state_event.return_value = nio.RoomGetStateEventError("unavailable", "M_UNKNOWN")
    assert await show() == "❌ Could not read this room's thread mode setting. Try again."


@pytest.mark.asyncio
async def test_thread_mode_command_requires_room_admin_for_set(tmp_path: Path) -> None:
    """Only Matrix room admins should be able to change a room thread mode override."""
    homeserver = _homeserver("@admin:localhost")
    client = homeserver.client()
    context = _thread_mode_context(tmp_path, client)
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "room"}, raw_text="!thread_mode room")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@user:localhost", "!thread_mode room"),
        command=command,
        requester_user_id="@user:localhost",
    )

    assert context.send_response.await_args.args[0] == "❌ Room admin only."
    assert resolve_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("thread_mode_published")
async def test_thread_mode_command_admin_sets_runtime_override_without_config_write(tmp_path: Path) -> None:
    """Room admins should set a durable runtime override without touching config.yaml."""
    homeserver = _homeserver("@admin:localhost")
    client = homeserver.client()
    context = _thread_mode_context(tmp_path, client)
    config_before = context.runtime_paths.config_path.read_text(encoding="utf-8")
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "room"}, raw_text="!thread_mode room")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@admin:localhost", "!thread_mode room"),
        command=command,
        requester_user_id="@admin:localhost",
    )

    assert "now uses `room`" in context.send_response.await_args.args[0]
    assert resolve_room_thread_mode_override(context.runtime_paths, ROOM_ID) == "room"
    assert homeserver.events[SETTING] == {"value": "room"}
    assert context.runtime_paths.config_path.read_text(encoding="utf-8") == config_before
    context.record_handled_turn.assert_called_once_with("$reply")


@pytest.mark.asyncio
@pytest.mark.usefixtures("thread_mode_published")
async def test_thread_mode_command_accepts_admin_sender_for_bridged_requester(tmp_path: Path) -> None:
    """The Matrix sender's room admin power must authorize a bridged requester's command."""
    homeserver = _homeserver("@bridge-admin:localhost")
    client = homeserver.client()
    context = _thread_mode_context(tmp_path, client)
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "room"}, raw_text="!thread_mode room")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@bridge-admin:localhost", "!thread_mode room"),
        command=command,
        requester_user_id="@puppet-user:localhost",
    )

    assert resolve_room_thread_mode_override(context.runtime_paths, ROOM_ID) == "room"


@pytest.mark.asyncio
@pytest.mark.usefixtures("thread_mode_published")
async def test_thread_mode_command_admin_reset_clears_runtime_override(tmp_path: Path) -> None:
    """Room admins should reset the runtime override back to static config behavior."""
    homeserver = _homeserver("@admin:localhost")
    client = homeserver.client()
    context = _thread_mode_context(tmp_path, client)
    homeserver.events[SETTING] = {"value": "room"}
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "reset"}, raw_text="!thread_mode reset")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@admin:localhost", "!thread_mode reset"),
        command=command,
        requester_user_id="@admin:localhost",
    )

    assert "override removed" in context.send_response.await_args.args[0]
    assert resolve_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None
    assert homeserver.events[SETTING] == {}


@pytest.mark.asyncio
async def test_thread_mode_command_fails_closed_when_power_levels_unavailable(tmp_path: Path) -> None:
    """Missing Matrix power-level state should not allow room mode changes."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
    context = _thread_mode_context(tmp_path, client)
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "thread"}, raw_text="!thread_mode thread")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@admin:localhost", "!thread_mode thread"),
        command=command,
        requester_user_id="@admin:localhost",
    )

    assert context.send_response.await_args.args[0] == "❌ Room admin only."
    assert resolve_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None
