"""Tests for MCP-aware orchestrator reload planning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from mindroom.mcp.manager import MCPServerManager
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from mindroom.orchestration.runtime import EntityStartResults
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config(tmp_path: Path, *, tool_name: str = "mcp_demo", command: str = "npx", required: bool = False) -> Config:
    return Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": command,
                    "required": required,
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": [tool_name],
                },
                "plain": {
                    "display_name": "Plain",
                    "role": "No MCP",
                },
            },
            "teams": {
                "dev_team": {
                    "display_name": "Dev Team",
                    "role": "Collaborate",
                    "agents": ["code"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )


def _config_with_code_agent(
    tmp_path: Path,
    *,
    role: str = "Write code",
    external_trigger_policy: dict[str, object] | None = None,
) -> Config:
    payload: dict[str, object] = {
        "agents": {
            "code": {
                "display_name": "Code",
                "role": role,
            },
        },
    }
    if external_trigger_policy is not None:
        payload["external_trigger_policy"] = external_trigger_policy
    return Config.validate_with_runtime(
        payload,
        _runtime_paths(tmp_path),
    )


def _trigger_snapshot(
    *,
    trigger_id: str = "campground",
    room_id: str = "!campground:example.org",
    enabled: bool = True,
) -> TriggerDeliverySnapshot:
    return TriggerDeliverySnapshot(
        trigger_id=trigger_id,
        uid=f"{trigger_id}-uid",
        version=1,
        auth_epoch=1,
        owner_user_id="@owner:example.org",
        description="",
        created_by_agent_name="code",
        created_in_room_id=room_id,
        target=ExternalTriggerTarget(room_id=room_id, agent="code"),
        resolved_room_id=room_id,
        auth="ed25519",
        public_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        public_key_fingerprint="sha256:test",
        key_id=f"{trigger_id}-main",
        allowed_kinds=(),
        replay_window_seconds=300,
        max_body_bytes=65536,
        enabled=enabled,
        config_generation=1,
        replay_scope=f"{trigger_id}:scope",
    )


def test_config_update_plan_restarts_only_entities_using_changed_mcp_server(tmp_path: Path) -> None:
    """Restart only the agents and teams that depend on the changed MCP server."""
    current_config = _config(tmp_path, command="npx")
    new_config = _config(tmp_path, command="node")
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        configured_entities={"router", "code", "plain", "dev_team"},
        existing_entities={"router", "code", "plain", "dev_team"},
        agent_bots={},
    )
    assert plan.changed_mcp_servers == {"demo"}
    assert "code" in plan.entities_to_restart
    assert "dev_team" in plan.entities_to_restart
    assert "plain" not in plan.entities_to_restart


def test_external_trigger_policy_change_does_not_restart_entities(tmp_path: Path) -> None:
    """Trigger policy is support config, not bot room membership."""
    plan = build_config_update_plan(
        current_config=_config_with_code_agent(tmp_path),
        new_config=_config_with_code_agent(
            tmp_path,
            external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
        ),
        configured_entities={ROUTER_AGENT_NAME, "code"},
        existing_entities={ROUTER_AGENT_NAME, "code"},
        agent_bots={},
    )

    assert plan.entities_to_restart == set()
    assert plan.only_support_service_changes is True


def _manager_with_failed_server(*, required: bool) -> MagicMock:
    manager = MagicMock(spec=MCPServerManager)
    manager.failed_server_ids.return_value = {"demo"}
    manager.failed_required_server_ids.return_value = {"demo"} if required else set()
    return manager


def test_entities_blocked_only_by_failed_required_mcp_servers(tmp_path: Path) -> None:
    """A failed optional MCP server must not block dependent entity startup."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    entity_names = {"code", "dev_team", "plain"}

    config = _config(tmp_path)
    orchestrator._mcp_manager = _manager_with_failed_server(required=False)
    assert orchestrator._entities_blocked_by_failed_mcp_servers(entity_names, config) == set()

    required_config = _config(tmp_path, required=True)
    orchestrator._mcp_manager = _manager_with_failed_server(required=True)
    assert orchestrator._entities_blocked_by_failed_mcp_servers(entity_names, required_config) == {
        "code",
        "dev_team",
    }


@pytest.mark.asyncio
async def test_start_entities_proceed_when_optional_mcp_server_failed(tmp_path: Path) -> None:
    """Bots referencing a failed optional MCP server start normally in degraded mode."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    bot = MagicMock(spec=AgentBot)
    orchestrator.agent_bots = {"code": bot}
    orchestrator._mcp_manager = _manager_with_failed_server(required=False)

    with patch.object(orchestrator, "_try_start_bot_once", new=AsyncMock(return_value=True)) as mock_try_start:
        results = await orchestrator._start_entities_once(["code"], start_sync_tasks=False)

    assert results.started_bots == [bot]
    assert results.retryable_entities == []
    mock_try_start.assert_awaited_once_with("code", bot)


@pytest.mark.asyncio
async def test_start_entities_marks_mcp_blocked_entities_retryable(tmp_path: Path) -> None:
    """Treat required MCP discovery outages as retryable startup failures, not permanent disablement."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path, required=True)
    orchestrator.agent_bots = {"code": MagicMock(spec=AgentBot)}
    orchestrator._mcp_manager = _manager_with_failed_server(required=True)

    with patch.object(orchestrator, "_try_start_bot_once", new=AsyncMock()) as mock_try_start:
        results = await orchestrator._start_entities_once(["code"], start_sync_tasks=False)

    assert results.retryable_entities == ["code"]
    assert results.permanently_failed_entities == []
    mock_try_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_start_attempt_does_not_bind_external_trigger_runtime_before_room_setup(tmp_path: Path) -> None:
    """Starting a router client is not enough to accept trigger delivery."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    router_bot = MagicMock(spec=AgentBot)
    router_bot.try_start = AsyncMock(return_value=True)

    with patch.object(orchestrator._external_trigger_runtime, "_bind_from_started_bots") as mock_bind:
        started = await orchestrator._try_start_bot_once(ROUTER_AGENT_NAME, router_bot)

    assert started is True
    mock_bind.assert_not_called()


def test_external_trigger_runtime_binds_router_with_live_readiness_gate(tmp_path: Path) -> None:
    """Trigger delivery runtime should bind the router with a live readiness gate."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config_with_code_agent(tmp_path)

    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()

    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.running = False
    target_bot.client = object()
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: router_bot,
        "code": target_bot,
    }

    with patch.object(orchestrator._external_trigger_runtime, "_bind_from_started_bots") as mock_bind:
        orchestrator._external_trigger_runtime.bind_if_ready(orchestrator.config, orchestrator.agent_bots)

    mock_bind.assert_called_once()
    assert mock_bind.call_args.args == ((router_bot,),)
    assert callable(mock_bind.call_args.kwargs["is_trigger_snapshot_ready"])


@pytest.mark.asyncio
async def test_external_trigger_readiness_is_per_trigger_room(tmp_path: Path) -> None:
    """One ready trigger room should not make same-agent triggers in other rooms ready."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config_with_code_agent(tmp_path)
    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.running = True
    target_client = object()
    target_bot.client = target_client
    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_client = object()
    router_bot.client = router_client
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    async def get_joined_room_ids(client: object) -> list[str]:
        assert client in {router_client, target_client}
        return ["!campground:example.org"]

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms", side_effect=get_joined_room_ids):
        assert await orchestrator._external_trigger_runtime.is_ready(
            _trigger_snapshot(trigger_id="campground", room_id="!campground:example.org"),
            orchestrator.agent_bots,
        )
        assert not await orchestrator._external_trigger_runtime.is_ready(
            _trigger_snapshot(trigger_id="campground_other", room_id="!other:example.org"),
            orchestrator.agent_bots,
        )


@pytest.mark.asyncio
async def test_external_trigger_readiness_uses_matrix_joined_rooms(tmp_path: Path) -> None:
    """Trigger readiness should require both router and target joined rooms."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config_with_code_agent(tmp_path)
    router_client = object()
    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.running = True
    target_client = object()
    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.client = target_client
    target_bot.running = True
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    async def get_joined_room_ids(client: object) -> list[str]:
        return {
            router_client: ["!campground:example.org"],
            target_client: ["!campground:example.org"],
        }[client]

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms", side_effect=get_joined_room_ids):
        assert await orchestrator._external_trigger_runtime.is_ready(
            _trigger_snapshot(),
            orchestrator.agent_bots,
        )


@pytest.mark.asyncio
async def test_external_trigger_api_sync_skips_when_embedded_api_disabled(tmp_path: Path) -> None:
    """No-API runs should not touch the bundled API app for trigger delivery."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), api_enabled=False)
    config = _config_with_code_agent(tmp_path)
    orchestrator.config = config
    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    with (
        patch("mindroom.api.config_lifecycle._publish_runtime_config_into_app") as mock_publish,
        patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind,
    ):
        await orchestrator._external_trigger_runtime.sync_api_config_snapshot(config, config)
        orchestrator._external_trigger_runtime.bind_if_ready(orchestrator.config, orchestrator.agent_bots)

    mock_publish.assert_not_called()
    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_startup_room_setup_binds_external_trigger_runtime_after_setup(tmp_path: Path) -> None:
    """Initial startup publishes trigger delivery only after room setup completes."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    bot = MagicMock(spec=AgentBot)
    call_order: list[str] = []

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == [bot]
        call_order.append("setup")

    def bind_runtime(_config: Config | None, _bots: object) -> None:
        call_order.append("bind")

    with (
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=setup_rooms),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready", side_effect=bind_runtime),
    ):
        await orchestrator._setup_startup_rooms_and_memberships([bot])

    assert call_order == ["setup", "bind"]


@pytest.mark.asyncio
async def test_trigger_support_only_reload_rebinds_external_trigger_runtime(tmp_path: Path) -> None:
    """Trigger-only reloads should publish a runtime bound to the new config generation."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
    )
    orchestrator.config = current_config
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
        "code": MagicMock(spec=AgentBot),
    }
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        configured_entities={ROUTER_AGENT_NAME, "code"},
        existing_entities={ROUTER_AGENT_NAME, "code"},
        agent_bots=orchestrator.agent_bots,
    )

    assert plan.only_support_service_changes is True

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator, "_stop_entities_before_mcp_sync", new=AsyncMock(return_value=set())),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_activate_hook_registry"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
        patch("mindroom.api.config_lifecycle._publish_runtime_config_into_app", return_value=True),
        patch.object(orchestrator, "_update_unchanged_bots", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator._approval_transport, "mark_startup_runtime_support_ready", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind_runtime,
    ):
        updated = await orchestrator._apply_config_update_plan(current_config, plan, ())

    assert updated is False
    mock_bind_runtime.assert_called_once_with(new_config, orchestrator.agent_bots)


@pytest.mark.asyncio
async def test_trigger_support_only_reload_publishes_api_config_before_binding_runtime(tmp_path: Path) -> None:
    """Runtime binding should use the API generation for the config being applied."""
    runtime_paths = _runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
    )
    runtime_paths.config_path.write_text(yaml.dump(current_config.authored_model_dump()), encoding="utf-8")
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    file_race_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 8192, "max_body_bytes": 8192},
    )
    runtime_paths.config_path.write_text(yaml.dump(file_race_config.authored_model_dump()), encoding="utf-8")
    orchestrator.config = current_config
    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    router_bot._conversation_cache = object()
    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.running = True
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: router_bot,
        "code": target_bot,
    }
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        configured_entities={ROUTER_AGENT_NAME, "code"},
        existing_entities={ROUTER_AGENT_NAME, "code"},
        agent_bots=orchestrator.agent_bots,
    )

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator, "_stop_entities_before_mcp_sync", new=AsyncMock(return_value=set())),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_activate_hook_registry"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
        patch.object(orchestrator, "_update_unchanged_bots", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator._approval_transport, "mark_startup_runtime_support_ready", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
    ):
        await orchestrator._apply_config_update_plan(current_config, plan, ())

    snapshot = config_lifecycle.require_api_state(api_main.app).snapshot
    runtime = config_lifecycle.app_state(api_main.app).external_trigger_runtime
    assert snapshot.runtime_config is not None
    assert snapshot.runtime_config.external_trigger_policy.default_max_body_bytes == 4096
    assert runtime is not None
    assert runtime.config_generation == snapshot.generation


@pytest.mark.asyncio
async def test_trigger_support_api_publish_runs_off_event_loop(tmp_path: Path) -> None:
    """Publishing trigger API snapshots does config IO off the orchestrator event loop."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
    )
    orchestrator.config = current_config

    with patch(
        "mindroom.orchestration.external_trigger_runtime.asyncio.to_thread",
        new=AsyncMock(return_value=True),
    ) as mock_to_thread:
        await orchestrator._external_trigger_runtime.sync_api_config_snapshot(current_config, new_config)

    mock_to_thread.assert_awaited_once()
    assert mock_to_thread.await_args.args == (
        api_main.config_lifecycle._publish_runtime_config_into_app,
        new_config,
        orchestrator.runtime_paths,
        api_main.app,
    )


@pytest.mark.asyncio
async def test_trigger_support_only_reload_unbinds_and_raises_when_api_publish_fails(tmp_path: Path) -> None:
    """Failed API snapshot publish must not leave trigger runtime bound to stale config."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
    )
    orchestrator.config = current_config
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
        "code": MagicMock(spec=AgentBot),
    }
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        configured_entities={ROUTER_AGENT_NAME, "code"},
        existing_entities={ROUTER_AGENT_NAME, "code"},
        agent_bots=orchestrator.agent_bots,
    )

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator, "_stop_entities_before_mcp_sync", new=AsyncMock(return_value=set())),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_activate_hook_registry"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
        patch("mindroom.api.config_lifecycle._publish_runtime_config_into_app", return_value=False),
        patch.object(orchestrator._external_trigger_runtime, "unbind") as mock_unbind_runtime,
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind_runtime,
        pytest.raises(RuntimeError, match="Failed to publish external trigger API config snapshot"),
    ):
        await orchestrator._apply_config_update_plan(current_config, plan, ())

    mock_unbind_runtime.assert_called_once_with()
    mock_bind_runtime.assert_not_called()


def test_log_mcp_degraded_entities_warns_per_failed_optional_server(tmp_path: Path) -> None:
    """Report only running entities as degraded by an unavailable optional MCP server."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    config = _config(tmp_path)
    orchestrator._mcp_manager = _manager_with_failed_server(required=False)
    running_bot = MagicMock(spec=AgentBot)
    running_bot.running = True
    stopped_bot = MagicMock(spec=AgentBot)
    stopped_bot.running = False
    orchestrator.agent_bots = {"code": running_bot, "dev_team": stopped_bot}

    with patch("mindroom.orchestrator.logger") as mock_logger:
        orchestrator._log_mcp_degraded_entities(config)

    mock_logger.warning.assert_called_once()
    kwargs = mock_logger.warning.call_args.kwargs
    assert kwargs["server_id"] == "demo"
    assert kwargs["degraded_entities"] == ["code"]


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_restarts_dependent_entities(tmp_path: Path) -> None:
    """Restart only MCP-dependent entities and keep retry scheduling intact."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    orchestrator.running = True

    with (
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock()) as mock_stop_entities,
        patch.object(orchestrator, "_cancel_bot_start_task", new=AsyncMock()) as mock_cancel,
        patch.object(
            orchestrator,
            "_create_and_start_entities",
            new=AsyncMock(return_value=EntityStartResults(retryable_entities=["code"])),
        ) as mock_create_and_start,
        patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
        patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    changed_entities = mock_create_and_start.await_args.args[0]
    assert changed_entities == {"code", "dev_team"}
    assert mock_stop_entities.await_args.args[0] == {"code", "dev_team"}
    assert mock_create_and_start.await_args.kwargs["start_sync_tasks"] is True
    assert {args.args[0] for args in mock_cancel.await_args_list} == {"code", "dev_team"}
    mock_schedule_retry.assert_awaited_once_with("code")
    mock_clear_snapshot_cache.assert_called_once_with()


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_sets_up_rooms_before_trigger_runtime_rebind(tmp_path: Path) -> None:
    """MCP restarts refresh rooms before publishing trigger runtime."""
    runtime_paths = _runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo"],
                },
            },
        },
        runtime_paths,
    )
    orchestrator.running = True
    started_bot = MagicMock(spec=AgentBot)
    call_order: list[str] = []

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == [started_bot]
        call_order.append("setup")

    def bind_runtime(_config: Config | None, _bots: object) -> None:
        call_order.append("bind")

    with (
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock()),
        patch.object(orchestrator, "_cancel_bot_start_task", new=AsyncMock()),
        patch.object(
            orchestrator,
            "_create_and_start_entities",
            new=AsyncMock(return_value=EntityStartResults(started_bots=[started_bot])),
        ),
        patch.object(orchestrator._external_trigger_runtime, "unbind"),
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=setup_rooms),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready", side_effect=bind_runtime),
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    assert call_order == ["setup", "bind"]


@pytest.mark.asyncio
async def test_router_restart_unbinds_external_trigger_runtime_before_stop_and_stays_unbound_on_failure(
    tmp_path: Path,
) -> None:
    """Router restarts should clear external trigger runtime before stopping the old client."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    config = _config(tmp_path)
    orchestrator.config = config
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: MagicMock(spec=AgentBot)}
    order: list[str] = []
    external_trigger_runtime_bound = True

    plan = build_config_update_plan(
        current_config=config,
        new_config=config,
        configured_entities={ROUTER_AGENT_NAME},
        existing_entities={ROUTER_AGENT_NAME},
        agent_bots=orchestrator.agent_bots,
    )
    plan = ConfigUpdatePlan(
        new_config=config,
        changed_mcp_servers=plan.changed_mcp_servers,
        configured_entities={ROUTER_AGENT_NAME},
        entities_to_restart={ROUTER_AGENT_NAME},
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    def unbind_external_trigger_runtime() -> None:
        nonlocal external_trigger_runtime_bound
        order.append("unbind")
        external_trigger_runtime_bound = False

    async def fake_stop_entities(*_args: object, **_kwargs: object) -> None:
        assert external_trigger_runtime_bound is False
        order.append("stop")

    async def fake_create_and_start_entities(*_args: object, **_kwargs: object) -> EntityStartResults:
        order.append("create")
        return EntityStartResults(retryable_entities=[ROUTER_AGENT_NAME])

    with (
        patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=unbind_external_trigger_runtime),
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock(side_effect=fake_stop_entities)),
        patch.object(orchestrator, "_create_and_start_entities", side_effect=fake_create_and_start_entities),
    ):
        (
            changed_entities,
            retryable_entities,
            permanently_failed_entities,
        ) = await orchestrator._restart_changed_entities(
            plan,
        )

    assert changed_entities == {ROUTER_AGENT_NAME}
    assert retryable_entities == [ROUTER_AGENT_NAME]
    assert permanently_failed_entities == []
    assert external_trigger_runtime_bound is False
    assert order == ["unbind", "stop", "create"]


@pytest.mark.asyncio
async def test_external_trigger_target_restart_unbinds_runtime_before_stop(tmp_path: Path) -> None:
    """Restarting a trigger target should make trigger delivery fail closed until rooms are reconciled."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    config = _config_with_code_agent(tmp_path)
    orchestrator.config = config
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
        "code": MagicMock(spec=AgentBot),
    }
    order: list[str] = []
    external_trigger_runtime_bound = True
    plan = ConfigUpdatePlan(
        new_config=config,
        changed_mcp_servers=set(),
        configured_entities={ROUTER_AGENT_NAME, "code"},
        entities_to_restart={"code"},
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    def unbind_external_trigger_runtime() -> None:
        nonlocal external_trigger_runtime_bound
        order.append("unbind")
        external_trigger_runtime_bound = False

    async def fake_stop_entities(*_args: object, **_kwargs: object) -> None:
        assert external_trigger_runtime_bound is False
        order.append("stop")

    async def fake_create_and_start_entities(*_args: object, **_kwargs: object) -> EntityStartResults:
        order.append("create")
        return EntityStartResults(started_bots=[orchestrator.agent_bots["code"]])

    with (
        patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=unbind_external_trigger_runtime),
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock(side_effect=fake_stop_entities)),
        patch.object(orchestrator, "_create_and_start_entities", side_effect=fake_create_and_start_entities),
    ):
        (
            changed_entities,
            retryable_entities,
            permanently_failed_entities,
        ) = await orchestrator._restart_changed_entities(plan)

    assert changed_entities == {"code"}
    assert retryable_entities == []
    assert permanently_failed_entities == []
    assert external_trigger_runtime_bound is False
    assert order == ["unbind", "stop", "create"]


@pytest.mark.asyncio
async def test_apply_config_update_plan_unbinds_runtime_before_restarted_entity_stop(
    tmp_path: Path,
) -> None:
    """Config reload should fail closed before stopping a restarted entity."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(tmp_path, role="Write better code")
    orchestrator.config = current_config
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
        "code": MagicMock(spec=AgentBot),
    }
    plan = ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        configured_entities={ROUTER_AGENT_NAME, "code"},
        entities_to_restart={"code"},
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )
    external_trigger_runtime_bound = True

    def unbind_external_trigger_runtime() -> None:
        nonlocal external_trigger_runtime_bound
        external_trigger_runtime_bound = False

    async def fake_stop_entities(*_args: object, **_kwargs: object) -> None:
        assert external_trigger_runtime_bound is False

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator, "_stop_entities_before_mcp_sync", new=AsyncMock(return_value=set())),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_activate_hook_registry"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
        patch("mindroom.api.config_lifecycle._publish_runtime_config_into_app", return_value=True),
        patch.object(orchestrator, "_update_unchanged_bots", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=unbind_external_trigger_runtime),
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock(side_effect=fake_stop_entities)),
        patch.object(orchestrator, "_create_and_start_entities", new=AsyncMock(return_value=EntityStartResults())),
        patch.object(orchestrator, "_reconcile_post_update_rooms", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator._approval_transport, "mark_startup_runtime_support_ready", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
    ):
        updated = await orchestrator._apply_config_update_plan(current_config, plan, ())

    assert updated is True
    assert external_trigger_runtime_bound is False


@pytest.mark.asyncio
async def test_router_removal_unbinds_external_trigger_runtime_before_cleanup(tmp_path: Path) -> None:
    """Router removal should clear external trigger runtime before cleaning up the old client."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    order: list[str] = []
    external_trigger_runtime_bound = True

    async def cleanup() -> None:
        assert external_trigger_runtime_bound is False
        order.append("cleanup")

    router_bot = MagicMock(spec=AgentBot)
    router_bot.cleanup = AsyncMock(side_effect=cleanup)
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    def unbind_external_trigger_runtime() -> None:
        nonlocal external_trigger_runtime_bound
        order.append("unbind")
        external_trigger_runtime_bound = False

    with patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=unbind_external_trigger_runtime):
        await orchestrator._remove_deleted_entities({ROUTER_AGENT_NAME})

    assert ROUTER_AGENT_NAME not in orchestrator.agent_bots
    assert external_trigger_runtime_bound is False
    assert order == ["unbind", "cleanup"]


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_serializes_overlapping_restarts(tmp_path: Path) -> None:
    """Do not run overlapping restart cycles when multiple MCP servers hit the same entity."""
    runtime_paths = _runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                },
                "other": {
                    "transport": "stdio",
                    "command": "npx",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo", "mcp_other"],
                },
            },
        },
        runtime_paths,
    )
    orchestrator.running = True

    first_restart_entered = asyncio.Event()
    allow_first_restart_to_finish = asyncio.Event()
    create_calls: list[set[str]] = []

    async def fake_create_and_start(
        entity_names: set[str],
        _config: Config,
        *,
        start_sync_tasks: bool,
    ) -> EntityStartResults:
        assert start_sync_tasks is True
        create_calls.append(set(entity_names))
        if len(create_calls) == 1:
            first_restart_entered.set()
            await allow_first_restart_to_finish.wait()
        return EntityStartResults()

    with (
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock()) as mock_stop_entities,
        patch.object(orchestrator, "_cancel_bot_start_task", new=AsyncMock()),
        patch.object(orchestrator, "_create_and_start_entities", side_effect=fake_create_and_start),
    ):
        first_task = asyncio.create_task(orchestrator._handle_mcp_catalog_change("demo"))
        await first_restart_entered.wait()
        second_task = asyncio.create_task(orchestrator._handle_mcp_catalog_change("other"))
        await asyncio.sleep(0)
        assert mock_stop_entities.await_count == 1
        allow_first_restart_to_finish.set()
        await first_task
        await second_task

    assert create_calls == [{"code"}, {"code"}]
    assert mock_stop_entities.await_count == 2


@pytest.mark.asyncio
async def test_update_config_stops_mcp_entities_before_syncing_manager(tmp_path: Path) -> None:
    """Stop bots that depend on changed MCP servers before manager sync removes those servers."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
        "code": MagicMock(spec=AgentBot),
    }
    updated_config = Config.validate_with_runtime(
        {
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                },
            },
        },
        _runtime_paths(tmp_path),
    )
    persist_entity_accounts(orchestrator.config, orchestrator.runtime_paths)
    persist_entity_accounts(updated_config, orchestrator.runtime_paths)
    call_order: list[str] = []

    async def fake_stop_entities(*_args: object, **_kwargs: object) -> None:
        call_order.append("stop")

    async def fake_sync_mcp_manager(_config: Config) -> set[str]:
        call_order.append("sync")
        return set()

    with (
        patch("mindroom.orchestration.config_lifecycle.load_config", return_value=updated_config),
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock(side_effect=fake_stop_entities)),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(side_effect=fake_sync_mcp_manager)),
        patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
        patch.object(
            orchestrator,
            "_restart_changed_entities",
            new=AsyncMock(return_value=(set(), [], [])),
        ),
        patch.object(orchestrator, "_reconcile_post_update_rooms", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
    ):
        await orchestrator.config_reload.update_config()

    assert call_order[:2] == ["stop", "sync"]
