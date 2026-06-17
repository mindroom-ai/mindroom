"""Tests for room-level thread mode overrides and chat command handling."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, command_parser
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.handled_turns import HandledTurnState
from mindroom.message_target import MessageTarget
from mindroom.room_thread_modes import (
    _get_room_thread_mode_override,
    _load_cache,
    _store_path,
    clear_room_thread_mode_override,
    set_room_thread_mode_override,
)
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!room:localhost"


def _runtime_bound_config(config: Config, tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(config, runtime_paths)


def _power_levels_response(
    *,
    users: dict[str, int],
    users_default: int = 0,
) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content={
            "users": users,
            "users_default": users_default,
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id=ROOM_ID,
    )


def _thread_mode_context(tmp_path: Path, client: AsyncMock) -> CommandHandlerContext:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return CommandHandlerContext(
        client=client,
        config=Config(),
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data"),
        logger=MagicMock(),
        conversation_cache=MagicMock(),
        event_cache=make_event_cache_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, None, "$event"),
        record_handled_turn=MagicMock(),
        send_response=AsyncMock(return_value="$reply"),
    )


def _thread_mode_event(sender: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(
        sender=sender,
        event_id="$event",
        body=body,
        source={"content": {"body": body}},
    )


def test_room_thread_mode_store_roundtrip(tmp_path: Path) -> None:
    """Room thread mode overrides should persist outside config.yaml."""
    runtime_paths = test_runtime_paths(tmp_path)

    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) is None

    set_room_thread_mode_override(
        runtime_paths,
        room_id=ROOM_ID,
        mode="room",
        set_by="@admin:localhost",
    )
    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) == "room"
    assert _get_room_thread_mode_override(runtime_paths, "!other:localhost") is None

    assert clear_room_thread_mode_override(runtime_paths, ROOM_ID) is True
    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) is None
    assert clear_room_thread_mode_override(runtime_paths, ROOM_ID) is False


def test_room_thread_mode_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A corrupt room-mode store should read as empty and be replaced on write."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) is None
    assert _load_cache[path] == (path.stat().st_mtime_ns, {})

    set_room_thread_mode_override(
        runtime_paths,
        room_id=ROOM_ID,
        mode="thread",
        set_by="@admin:localhost",
    )
    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) == "thread"


def test_room_thread_mode_store_drops_invalid_records(tmp_path: Path) -> None:
    """Invalid persisted mode records should be ignored."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                ROOM_ID: {"mode": "invalid", "set_at": "2026-06-17T00:00:00+00:00"},
                "!other:localhost": {"mode": "room", "set_at": 12345},
            },
        ),
        encoding="utf-8",
    )

    assert _get_room_thread_mode_override(runtime_paths, ROOM_ID) is None
    assert _get_room_thread_mode_override(runtime_paths, "!other:localhost") is None


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
        set_by="@admin:localhost",
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


@pytest.mark.asyncio
async def test_thread_mode_command_requires_room_admin_for_set(tmp_path: Path) -> None:
    """Only Matrix room admins should be able to change a room thread mode override."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
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
    assert _get_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None


@pytest.mark.asyncio
async def test_thread_mode_command_admin_sets_runtime_override_without_config_write(tmp_path: Path) -> None:
    """Room admins should set a durable runtime override without touching config.yaml."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
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
    assert _get_room_thread_mode_override(context.runtime_paths, ROOM_ID) == "room"
    assert context.runtime_paths.config_path.read_text(encoding="utf-8") == config_before
    context.record_handled_turn.assert_called_once_with(
        HandledTurnState.from_source_event_id("$event", response_event_id="$reply"),
    )


@pytest.mark.asyncio
async def test_thread_mode_command_admin_reset_clears_runtime_override(tmp_path: Path) -> None:
    """Room admins should reset the runtime override back to static config behavior."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
    context = _thread_mode_context(tmp_path, client)
    set_room_thread_mode_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        mode="room",
        set_by="@admin:localhost",
    )
    command = Command(type=CommandType.THREAD_MODE, args={"args_text": "reset"}, raw_text="!thread_mode reset")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_thread_mode_event("@admin:localhost", "!thread_mode reset"),
        command=command,
        requester_user_id="@admin:localhost",
    )

    assert "override removed" in context.send_response.await_args.args[0]
    assert _get_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None


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
    assert _get_room_thread_mode_override(context.runtime_paths, ROOM_ID) is None
