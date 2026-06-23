"""Tests for MCP-aware orchestrator reload planning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
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


def _config_with_external_trigger(tmp_path: Path) -> Config:
    return Config.validate_with_runtime(
        {
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                },
            },
            "external_triggers": {
                "campground": {
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "target": {
                        "room_id": "!campground:example.org",
                        "agent": "code",
                    },
                },
            },
        },
        _runtime_paths(tmp_path),
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


def test_external_trigger_room_change_restarts_router_and_target_agent(tmp_path: Path) -> None:
    """Trigger target room changes affect both router and target bot room membership."""

    def config_with_trigger_room(room_id: str) -> Config:
        return Config.validate_with_runtime(
            {
                "agents": {
                    "code": {
                        "display_name": "Code",
                        "role": "Write code",
                    },
                },
                "external_triggers": {
                    "campground": {
                        "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                        "target": {
                            "room_id": room_id,
                            "agent": "code",
                        },
                    },
                },
            },
            _runtime_paths(tmp_path),
        )

    plan = build_config_update_plan(
        current_config=config_with_trigger_room("!old:example.org"),
        new_config=config_with_trigger_room("!new:example.org"),
        configured_entities={ROUTER_AGENT_NAME, "code"},
        existing_entities={ROUTER_AGENT_NAME, "code"},
        agent_bots={},
    )

    assert {ROUTER_AGENT_NAME, "code"} <= plan.entities_to_restart


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

    with patch.object(orchestrator, "_bind_external_trigger_runtime_from_started_bots") as mock_bind:
        started = await orchestrator._try_start_bot_once(ROUTER_AGENT_NAME, router_bot)

    assert started is True
    mock_bind.assert_not_called()


def test_external_trigger_runtime_waits_for_running_trigger_targets(tmp_path: Path) -> None:
    """Trigger delivery stays unavailable until every enabled target bot is running."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config_with_external_trigger(tmp_path)

    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()

    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.running = False
    orchestrator.agent_bots = {
        ROUTER_AGENT_NAME: router_bot,
        "code": target_bot,
    }

    with patch.object(orchestrator, "_bind_external_trigger_runtime_from_started_bots") as mock_bind:
        orchestrator._bind_external_trigger_runtime_if_ready()
        target_bot.running = True
        orchestrator._bind_external_trigger_runtime_if_ready()

    mock_bind.assert_called_once_with((router_bot,))


@pytest.mark.asyncio
async def test_startup_room_setup_binds_external_trigger_runtime_after_setup(tmp_path: Path) -> None:
    """Initial startup publishes trigger delivery only after room setup completes."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    bot = MagicMock(spec=AgentBot)
    call_order: list[str] = []

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == [bot]
        call_order.append("setup")

    def bind_runtime() -> None:
        call_order.append("bind")

    with (
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=setup_rooms),
        patch.object(orchestrator, "_bind_external_trigger_runtime_if_ready", side_effect=bind_runtime),
    ):
        await orchestrator._setup_startup_rooms_and_memberships([bot])

    assert call_order == ["setup", "bind"]


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
        patch.object(orchestrator, "_unbind_external_trigger_runtime", side_effect=unbind_external_trigger_runtime),
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
    config = _config_with_external_trigger(tmp_path)
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
        patch.object(orchestrator, "_unbind_external_trigger_runtime", side_effect=unbind_external_trigger_runtime),
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

    with patch.object(orchestrator, "_unbind_external_trigger_runtime", side_effect=unbind_external_trigger_runtime):
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
