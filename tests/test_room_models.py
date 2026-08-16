"""Tests for durable room-level model overrides."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.routing
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, command_parser, get_command_help
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.history.runtime import close_team_runtime_state_dbs
from mindroom.message_target import MessageTarget
from mindroom.room_model_overrides import (
    _store_path,
    clear_room_model_override,
    resolve_room_model_override,
    set_room_model_override,
)
from mindroom.teams import materialize_exact_team_members
from mindroom.thread_models import set_thread_model_override
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!room:localhost"


@dataclass(frozen=True)
class _RoomModelEvent:
    sender: str
    event_id: str
    body: str
    source: dict[str, dict[str, str]]


def _power_levels_response(*, users: dict[str, int]) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content={"users": users, "users_default": 0},
        event_type="m.room.power_levels",
        state_key="",
        room_id=ROOM_ID,
    )


def _room_model_context(
    tmp_path: Path,
    client: AsyncMock,
    *,
    models: dict[str, ModelConfig] | None = None,
) -> CommandHandlerContext:
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", model="default")},
            models=models
            or {
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    return make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )


def _room_model_event(sender: str, body: str) -> _RoomModelEvent:
    return _RoomModelEvent(
        sender=sender,
        event_id="$event",
        body=body,
        source={"content": {"body": body}},
    )


@pytest.mark.parametrize(
    ("message", "expected_args"),
    [
        ("!room_model", ""),
        ("!room-model large", "large"),
        ("!roommodel reset", "reset"),
    ],
)
def test_room_model_command_parses_supported_spellings(message: str, expected_args: str) -> None:
    """Missing room-model parser support must not route valid commands as unknown."""
    command = command_parser.parse(message)

    assert command is not None
    assert command.type.value == "room_model"
    assert command.args["args_text"] == expected_args


@pytest.mark.parametrize("topic", ["room_model", "room-model", "roommodel"])
def test_room_model_help_accepts_supported_spellings(topic: str) -> None:
    """Missing help routing must not hide room-model scope and permissions."""
    help_text = get_command_help(topic)

    assert "**Room Model Command**" in help_text
    assert "room admin" in help_text.lower()
    assert "room default returns to configured room or entity models" in help_text.lower()
    assert "Thread overrides take precedence" in help_text


@pytest.mark.asyncio
async def test_room_model_command_sets_runtime_default_without_writing_config(tmp_path: Path) -> None:
    """Missing room state must not leave future room turns on entity config."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
    context = _room_model_context(tmp_path, client)
    config_before = context.runtime_paths.config_path.read_text(encoding="utf-8")
    command = Command(type=CommandType.ROOM_MODEL, args={"args_text": "large"}, raw_text="!room_model large")

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@admin:localhost", "!room_model large"),
        command=command,
        requester_user_id="@admin:localhost",
    )

    runtime_model = context.config.resolve_runtime_model(
        entity_name="assistant",
        room_id=ROOM_ID,
        runtime_paths=context.runtime_paths,
    )
    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000
    assert context.runtime_paths.config_path.read_text(encoding="utf-8") == config_before


@pytest.mark.asyncio
async def test_room_model_command_reset_restores_configured_default(tmp_path: Path) -> None:
    """Missing reset support must not leave a removed room choice active."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
    context = _room_model_context(tmp_path, client)

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@admin:localhost", "!room_model large"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "large"},
            raw_text="!room_model large",
        ),
        requester_user_id="@admin:localhost",
    )
    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@admin:localhost", "!room_model reset"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "reset"},
            raw_text="!room_model reset",
        ),
        requester_user_id="@admin:localhost",
    )

    runtime_model = context.config.resolve_runtime_model(
        entity_name="assistant",
        room_id=ROOM_ID,
        runtime_paths=context.runtime_paths,
    )
    assert runtime_model.model_name == "default"
    response = context.send_response.await_args.args[0]
    assert "override removed" in response
    assert "Room default returns to configured room or entity models" in response
    assert "Thread model overrides still take precedence" in response


@pytest.mark.asyncio
async def test_room_model_command_show_lists_state_and_available_models(tmp_path: Path) -> None:
    """Missing status support must not hide room state or valid model choices."""
    client = AsyncMock()
    context = _room_model_context(tmp_path, client)

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@user:localhost", "!room_model"),
        command=Command(type=CommandType.ROOM_MODEL, args={"args_text": ""}, raw_text="!room_model"),
        requester_user_id="@user:localhost",
    )

    response = context.send_response.await_args.args[0]
    assert "No room model override" in response
    assert "configured room or entity models define the room default" in response
    assert "Thread model overrides still take precedence" in response
    assert "`default` (openai default-model)" in response
    assert "`large` (openai large-model)" in response


def test_room_model_store_scopes_records_by_room_and_preserves_audit_metadata(tmp_path: Path) -> None:
    """A room override must not leak into another room or lose its author."""
    runtime_paths = test_runtime_paths(tmp_path)

    set_room_model_override(
        runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
        set_by="@admin:localhost",
    )

    state = resolve_room_model_override(runtime_paths, ROOM_ID, configured_models={"default", "large"})
    other_state = resolve_room_model_override(
        runtime_paths,
        "!other:localhost",
        configured_models={"default", "large"},
    )
    assert state.active == "large"
    assert state.stale is None
    assert state.set_by == "@admin:localhost"
    assert state.set_at is not None
    assert other_state.active is None
    assert clear_room_model_override(runtime_paths, ROOM_ID) is True
    assert clear_room_model_override(runtime_paths, ROOM_ID) is False


def test_room_model_overrides_do_not_silently_evict_active_rooms(tmp_path: Path) -> None:
    """A durable room default must remain until reset, even after many other rooms are set."""
    context = _room_model_context(tmp_path, AsyncMock())
    for index in range(1001):
        set_room_model_override(
            context.runtime_paths,
            room_id=f"!room-{index}:localhost",
            model_name="large",
            set_by="@admin:localhost",
        )

    oldest = resolve_room_model_override(
        context.runtime_paths,
        "!room-0:localhost",
        configured_models=context.config.models,
    )

    assert oldest.active == "large"


def test_room_model_store_ignores_corrupt_records_and_classifies_removed_models(tmp_path: Path) -> None:
    """Malformed or removed model names must never become active runtime choices."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                ROOM_ID: {"model": [], "set_at": "2026-08-15T00:00:00+00:00"},
                "!bad-time:localhost": {"model": "large", "set_at": 12345},
                "!removed:localhost": {"model": "removed", "set_at": "2026-08-15T00:00:00+00:00"},
            },
        ),
        encoding="utf-8",
    )

    invalid = resolve_room_model_override(runtime_paths, ROOM_ID, configured_models={"default", "large"})
    invalid_time = resolve_room_model_override(
        runtime_paths,
        "!bad-time:localhost",
        configured_models={"default", "large"},
    )
    stale = resolve_room_model_override(
        runtime_paths,
        "!removed:localhost",
        configured_models={"default", "large"},
    )
    assert invalid.active is None
    assert invalid.stale is None
    assert invalid_time.active is None
    assert invalid_time.stale is None
    assert stale.active is None
    assert stale.stale == "removed"


def test_runtime_room_override_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Room runtime state must beat static room config while thread and explicit choices still win."""
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", model="default")},
            room_models={"lobby": "default"},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    set_room_model_override(
        runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
        set_by="@admin:localhost",
    )

    assert (
        config.resolve_runtime_model(
            entity_name="assistant",
            room_id=ROOM_ID,
            runtime_paths=runtime_paths,
        ).model_name
        == "large"
    )

    thread_id = "$thread:localhost"
    set_thread_model_override(
        runtime_paths,
        thread_id=thread_id,
        model_name="default",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    assert (
        config.resolve_runtime_model(
            entity_name="assistant",
            room_id=ROOM_ID,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        ).model_name
        == "default"
    )
    assert (
        config.resolve_runtime_model(
            entity_name="assistant",
            active_model_name="large",
            room_id=ROOM_ID,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        ).model_name
        == "large"
    )


@pytest.mark.asyncio
async def test_runtime_room_override_selects_router_model(tmp_path: Path) -> None:
    """Router selection must not bypass the room's runtime model default."""
    context = _room_model_context(tmp_path, AsyncMock())
    set_room_model_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
        set_by="@admin:localhost",
    )
    selected_models: list[str] = []

    def load_model(_config: Config, _runtime_paths: object, model_name: str) -> MagicMock:
        selected_models.append(model_name)
        return MagicMock(id=f"{model_name}-model")

    router = AsyncMock()
    router.arun.return_value = SimpleNamespace(
        content={"entity_name": "assistant", "reasoning": "Only candidate"},
    )
    with (
        patch("mindroom.routing.model_loading.get_model_instance", side_effect=load_model),
        patch("mindroom.routing.Agent", return_value=router),
    ):
        result = await mindroom.routing.suggest_responder(
            "Help me",
            ["assistant"],
            context.config,
            context.runtime_paths,
            room_id=ROOM_ID,
        )

    assert result == "assistant"
    assert selected_models == ["large"]


def test_runtime_model_precedence_applies_to_materialized_team_members(tmp_path: Path) -> None:
    """Team members must use room defaults while retaining higher-priority thread choices."""
    context = _room_model_context(tmp_path, AsyncMock())
    set_room_model_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
        set_by="@admin:localhost",
    )

    members = materialize_exact_team_members(
        ["assistant"],
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="team",
            requester_id="@user:localhost",
            room_id=ROOM_ID,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
    )
    try:
        assert members.agents[0].model is not None
        assert members.agents[0].model.id == "large-model"
    finally:
        close_team_runtime_state_dbs(agents=members.agents, team_db=None)

    thread_id = "$thread:localhost"
    set_thread_model_override(
        context.runtime_paths,
        thread_id=thread_id,
        model_name="default",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    thread_members = materialize_exact_team_members(
        ["assistant"],
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="team",
            requester_id="@user:localhost",
            room_id=ROOM_ID,
            thread_id=thread_id,
            resolved_thread_id=thread_id,
            session_id=f"{ROOM_ID}:{thread_id}",
        ),
    )
    try:
        assert thread_members.agents[0].model is not None
        assert thread_members.agents[0].model.id == "default-model"
    finally:
        close_team_runtime_state_dbs(agents=thread_members.agents, team_db=None)


def test_stale_runtime_room_override_falls_back_to_static_room_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed runtime aliases must not mask a still-valid static room model."""
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", model="large")},
            room_models={"lobby": "default"},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model"),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    set_room_model_override(
        runtime_paths,
        room_id=ROOM_ID,
        model_name="removed",
        set_by="@admin:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="assistant",
        room_id=ROOM_ID,
        runtime_paths=runtime_paths,
    )
    assert runtime_model.model_name == "default"


@pytest.mark.asyncio
async def test_room_model_command_rejects_non_admin_without_changing_state(tmp_path: Path) -> None:
    """A regular room member must not change defaults for every conversation."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
    context = _room_model_context(tmp_path, client)

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@user:localhost", "!room_model large"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "large"},
            raw_text="!room_model large",
        ),
        requester_user_id="@user:localhost",
    )

    state = resolve_room_model_override(context.runtime_paths, ROOM_ID, configured_models=context.config.models)
    assert state.active is None
    assert context.send_response.await_args.args[0] == "❌ Room admin only."


@pytest.mark.asyncio
async def test_room_model_command_records_authorizing_sender(tmp_path: Path) -> None:
    """Bridge-mediated commands must audit the Matrix user satisfying the admin check."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@bridge-admin:localhost": 100})
    context = _room_model_context(tmp_path, client)

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@bridge-admin:localhost", "!room_model large"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "large"},
            raw_text="!room_model large",
        ),
        requester_user_id="@puppet-user:localhost",
    )

    state = resolve_room_model_override(context.runtime_paths, ROOM_ID, configured_models=context.config.models)
    assert state.active == "large"
    assert state.set_by == "@bridge-admin:localhost"


@pytest.mark.asyncio
async def test_room_model_command_reports_stale_override(tmp_path: Path) -> None:
    """Status must distinguish an ignored removed alias from no stored choice."""
    client = AsyncMock()
    context = _room_model_context(tmp_path, client)
    set_room_model_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        model_name="removed",
        set_by="@admin:localhost",
    )

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@user:localhost", "!room_model status"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "status"},
            raw_text="!room_model status",
        ),
        requester_user_id="@user:localhost",
    )

    assert "Stored room model override `removed` is unavailable and ignored" in context.send_response.await_args.args[0]


@pytest.mark.asyncio
async def test_room_model_command_configured_reset_name_sets_model(tmp_path: Path) -> None:
    """A configured model named reset must remain selectable instead of clearing state."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _power_levels_response(users={"@admin:localhost": 100})
    context = _room_model_context(
        tmp_path,
        client,
        models={
            "default": ModelConfig(provider="openai", id="default-model"),
            "reset": ModelConfig(provider="openai", id="reset-model"),
        },
    )

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@admin:localhost", "!room_model reset"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "reset"},
            raw_text="!room_model reset",
        ),
        requester_user_id="@admin:localhost",
    )

    state = resolve_room_model_override(context.runtime_paths, ROOM_ID, configured_models=context.config.models)
    assert state.active == "reset"


@pytest.mark.asyncio
async def test_room_model_command_unknown_name_lists_available_models(tmp_path: Path) -> None:
    """An invalid choice must leave state unchanged and provide actionable choices."""
    client = AsyncMock()
    context = _room_model_context(tmp_path, client)

    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_room_model_event("@user:localhost", "!room_model missing"),
        command=Command(
            type=CommandType.ROOM_MODEL,
            args={"args_text": "missing"},
            raw_text="!room_model missing",
        ),
        requester_user_id="@user:localhost",
    )

    response = context.send_response.await_args.args[0]
    state = resolve_room_model_override(context.runtime_paths, ROOM_ID, configured_models=context.config.models)
    assert state.active is None
    assert "Unknown model `missing`" in response
    assert "`default` (openai default-model)" in response
    assert "`large` (openai large-model)" in response
