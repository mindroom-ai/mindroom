"""Tests for hook-facing Matrix admin helpers."""

from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import HookContext, HookContextSupport
from mindroom.hooks.registry import HookRegistry, HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.matrix.cache import AgentMessageSnapshot
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import (
    bind_runtime_paths,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="test", id="test-model")}),
        runtime_paths,
    )


def _matrix_admin_module() -> object:
    spec = importlib.util.find_spec("mindroom.hooks.matrix_admin")
    assert spec is not None, "mindroom.hooks.matrix_admin should exist"
    return importlib.import_module("mindroom.hooks.matrix_admin")


def test_hooks_package_reexports_hook_matrix_admin_api() -> None:
    """The public hooks package should export the matrix admin hook API."""
    hooks = importlib.import_module("mindroom.hooks")

    assert hasattr(hooks, "HookMatrixAdmin")
    assert hasattr(hooks, "build_hook_matrix_admin")


def test_hook_context_declares_matrix_admin_field() -> None:
    """HookContext should expose a bound matrix admin helper."""
    assert "matrix_admin" in HookContext.__dataclass_fields__


def test_hook_context_declares_agent_message_snapshot_reader_field() -> None:
    """HookContext should expose a bound agent-message snapshot reader."""
    assert "agent_message_snapshot_reader" in HookContext.__dataclass_fields__


@pytest.mark.asyncio
async def test_hook_context_delegates_latest_agent_message_snapshot_reads(tmp_path: Path) -> None:
    """HookContext should route latest-agent-message snapshot reads through the bound helper."""
    config = _config(tmp_path)
    reader = AsyncMock(
        return_value=AgentMessageSnapshot(
            content={"body": "Working...", "msgtype": "m.text"},
            origin_server_ts=2000,
        ),
    )
    context = HookContext(
        event_name="message:enrich",
        plugin_name="workloop",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_matrix_admin"),
        correlation_id="corr-snapshot",
        runtime_started_at=1234.0,
        agent_message_snapshot_reader=reader,
    )

    snapshot = await context.get_latest_agent_message_snapshot(
        "!room:localhost",
        "@agent:localhost",
        thread_id="$thread_root",
    )

    assert snapshot == AgentMessageSnapshot(
        content={"body": "Working...", "msgtype": "m.text"},
        origin_server_ts=2000,
    )
    reader.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread_root",
        sender="@agent:localhost",
        runtime_started_at=1234.0,
    )


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_resolve_alias_returns_room_id(tmp_path: Path) -> None:
    """Alias resolution should return the resolved room ID on success."""
    module = _matrix_admin_module()
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#personal-user:localhost",
        room_id="!personal:localhost",
        servers=["localhost"],
    )

    admin = module.build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))
    room_id = await admin.resolve_alias("#personal-user:localhost")

    assert room_id == "!personal:localhost"
    client.room_resolve_alias.assert_awaited_once_with("#personal-user:localhost")


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_resolve_alias_returns_none_on_error(tmp_path: Path) -> None:
    """Alias resolution should fail closed on Matrix error responses."""
    module = _matrix_admin_module()
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")

    admin = module.build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))
    room_id = await admin.resolve_alias("#personal-user:localhost")

    assert room_id is None


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_delegates_existing_room_helpers(tmp_path: Path) -> None:
    """The hook builder should reuse the existing Matrix helper functions."""
    module = _matrix_admin_module()
    runtime_paths = runtime_paths_for(_config(tmp_path))
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"

    with (
        patch(
            "mindroom.hooks.matrix_admin.create_room",
            new=AsyncMock(return_value="!created:localhost"),
        ) as mock_create,
        patch("mindroom.hooks.matrix_admin.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
        patch(
            "mindroom.hooks.matrix_admin.get_room_members",
            new=AsyncMock(return_value={"@user:localhost", "@mindroom_router:localhost"}),
        ) as mock_members,
        patch("mindroom.hooks.matrix_admin.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
    ):
        admin = module.build_hook_matrix_admin(client, runtime_paths=runtime_paths)

        room_id = await admin.create_room(name="Personal Room", alias_localpart="personal-user", topic="Hello")
        invited = await admin.invite_user("!created:localhost", "@user:localhost")
        members = await admin.get_room_members("!created:localhost")
        added = await admin.add_room_to_space("!space:localhost", "!created:localhost")

    assert room_id == "!created:localhost"
    assert invited is True
    assert members == {"@user:localhost", "@mindroom_router:localhost"}
    assert added is True
    mock_create.assert_awaited_once_with(
        client=client,
        name="Personal Room",
        alias="personal-user",
        topic="Hello",
        power_users=None,
    )
    mock_invite.assert_awaited_once_with(client, "!created:localhost", "@user:localhost")
    mock_members.assert_awaited_once_with(client, "!created:localhost")
    mock_add.assert_awaited_once()


def test_hook_context_support_prefers_orchestrator_router_matrix_admin(tmp_path: Path) -> None:
    """Router hook support should reuse the orchestrator router admin surface when available."""
    config = _config(tmp_path)
    orchestrator = MagicMock()
    sentinel = object()
    orchestrator.hook_matrix_admin.return_value = sentinel
    runtime = SimpleNamespace(
        client=AsyncMock(spec=nio.AsyncClient),
        orchestrator=orchestrator,
        config=config,
        runtime_started_at=0.0,
    )
    support = HookContextSupport(
        runtime=runtime,
        logger=get_logger("tests.hook_matrix_admin"),
        runtime_paths=runtime_paths_for(config),
        agent_name="router",
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )

    assert hasattr(support, "matrix_admin")
    with patch("mindroom.hooks.matrix_admin.build_hook_matrix_admin", return_value=sentinel) as mock_build:
        admin = support.matrix_admin()

    assert admin is sentinel
    orchestrator.hook_matrix_admin.assert_called_once_with()
    mock_build.assert_not_called()


def test_hook_context_support_builds_router_matrix_admin_without_orchestrator(tmp_path: Path) -> None:
    """Router hook support should build from the live router client when no orchestrator exists."""
    config = _config(tmp_path)
    runtime = SimpleNamespace(
        client=AsyncMock(spec=nio.AsyncClient),
        orchestrator=None,
        config=config,
        runtime_started_at=0.0,
    )
    support = HookContextSupport(
        runtime=runtime,
        logger=get_logger("tests.hook_matrix_admin"),
        runtime_paths=runtime_paths_for(config),
        agent_name="router",
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )
    sentinel = object()

    assert hasattr(support, "matrix_admin")
    with patch("mindroom.hooks.matrix_admin.build_hook_matrix_admin", return_value=sentinel) as mock_build:
        admin = support.matrix_admin()

    assert admin is sentinel
    mock_build.assert_called_once_with(runtime.client, runtime_paths_for(config))


def test_hook_context_support_falls_back_to_orchestrator_router_matrix_admin(tmp_path: Path) -> None:
    """Non-router hooks should use the orchestrator router admin surface."""
    config = _config(tmp_path)
    orchestrator = MagicMock()
    sentinel = object()
    orchestrator.hook_matrix_admin.return_value = sentinel
    runtime = SimpleNamespace(
        client=AsyncMock(spec=nio.AsyncClient),
        orchestrator=orchestrator,
        config=config,
        runtime_started_at=0.0,
    )
    support = HookContextSupport(
        runtime=runtime,
        logger=get_logger("tests.hook_matrix_admin"),
        runtime_paths=runtime_paths_for(config),
        agent_name="code",
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )

    assert hasattr(support, "matrix_admin")
    admin = support.matrix_admin()

    assert admin is sentinel
    orchestrator.hook_matrix_admin.assert_called_once_with()


def test_hook_context_support_returns_none_without_router_matrix_admin(tmp_path: Path) -> None:
    """When no router admin client is available, matrix_admin should be unavailable."""
    config = _config(tmp_path)
    runtime = SimpleNamespace(
        client=None,
        orchestrator=None,
        config=config,
        runtime_started_at=0.0,
    )
    support = HookContextSupport(
        runtime=runtime,
        logger=get_logger("tests.hook_matrix_admin"),
        runtime_paths=runtime_paths_for(config),
        agent_name="code",
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=AsyncMock(),
    )

    assert hasattr(support, "matrix_admin")
    assert support.matrix_admin() is None


@pytest.mark.asyncio
async def test_emit_config_reloaded_context_includes_matrix_admin(tmp_path: Path) -> None:
    """config:reloaded should expose the router-backed matrix admin helper."""
    runtime_paths = orchestrator_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="test", id="test-model")}),
        runtime_paths,
    )
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    orchestrator.hook_registry = MagicMock()
    orchestrator.hook_registry.has_hooks.return_value = True
    router_client = AsyncMock(spec=nio.AsyncClient)
    router_client.homeserver = "http://localhost:8008"
    orchestrator.agent_bots["router"] = SimpleNamespace(
        client=router_client,
        _hook_send_message=AsyncMock(),
    )

    with patch("mindroom.orchestrator.emit", new=AsyncMock()) as mock_emit:
        await orchestrator._emit_config_reloaded(
            new_config=config,
            changed_entities={"router"},
            added_entities=set(),
            removed_entities=set(),
            plugin_changes=(),
        )

    context = mock_emit.await_args.args[2]
    assert context.matrix_admin is not None
