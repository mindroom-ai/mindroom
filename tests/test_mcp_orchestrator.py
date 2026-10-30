"""Tests for MCP-aware orchestrator reload planning."""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.api import main as api_main
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from mindroom.integrations import unbind_integrations
from mindroom.mcp.manager import MCPServerManager
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan, configured_entity_names
from mindroom.orchestration.entity_fleet import EntityStartResults
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.slack.integration import bound_slack_runtime
from tests.conftest import install_bots, published_config_load, swap_fleet_bot
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config(tmp_path: Path, *, tool_name: str = "mcp_demo", command: str = "npx", required: bool = False) -> Config:
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
        "models": {"default": {"provider": "openai", "id": "test-model"}},
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
        capability_token_hash=None,
        delivery_mode="reusable",
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
        bots=dict.fromkeys(configured_entity_names(new_config), MagicMock(running=True)),
    )
    assert "code" in plan.restart
    assert "dev_team" in plan.restart
    assert "plain" not in plan.restart


def test_external_trigger_policy_change_does_not_restart_entities(tmp_path: Path) -> None:
    """Trigger policy is support config, not bot room membership."""
    plan = build_config_update_plan(
        current_config=_config_with_code_agent(tmp_path),
        new_config=_config_with_code_agent(
            tmp_path,
            external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
        ),
        bots=dict.fromkeys(configured_entity_names(_config_with_code_agent(tmp_path)), MagicMock(running=True)),
    )

    assert plan.restart == set()


def _manager_with_failed_server(*, required: bool) -> MagicMock:
    manager = MagicMock(spec=MCPServerManager)
    manager.failed_server_ids.return_value = {"demo"}
    manager.failed_required_server_ids.return_value = {"demo"} if required else set()
    return manager


def test_entities_blocked_only_by_failed_required_mcp_servers(tmp_path: Path) -> None:
    """A failed optional MCP server must not block dependent entity startup."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    entity_names = {"code", "dev_team", "plain"}

    orchestrator._mcp_manager = _manager_with_failed_server(required=False)
    assert orchestrator._entities_blocked_by_failed_mcp_servers(entity_names) == set()

    orchestrator.config = _config(tmp_path, required=True)
    orchestrator._mcp_manager = _manager_with_failed_server(required=True)
    assert orchestrator._entities_blocked_by_failed_mcp_servers(entity_names) == {
        "code",
        "dev_team",
    }


@pytest.mark.asyncio
async def test_fleet_starts_entities_whose_optional_mcp_server_failed(tmp_path: Path) -> None:
    """Bots referencing a failed optional MCP server start normally in degraded mode."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    bot = MagicMock(spec=AgentBot)
    install_bots(orchestrator, {"code": bot})
    orchestrator._mcp_manager = _manager_with_failed_server(required=False)

    results = await orchestrator.fleet.try_start(["code"])

    assert results.started_bots == [bot]
    assert results.retryable_entities == []
    bot.start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_fleet_marks_entities_blocked_by_required_mcp_servers_retryable(tmp_path: Path) -> None:
    """Treat required MCP discovery outages as retryable startup failures, not permanent disablement."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config(tmp_path, required=True),
    )
    bot = MagicMock(spec=AgentBot)
    install_bots(orchestrator, {"code": bot})
    orchestrator._mcp_manager = _manager_with_failed_server(required=True)

    results = await orchestrator.fleet.try_start(["code"])

    assert results.retryable_entities == ["code"]
    assert results.permanently_failed_entities == []
    assert not orchestrator.fleet.is_disabled("code")
    bot.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_start_does_not_bind_external_trigger_runtime_before_room_setup(tmp_path: Path) -> None:
    """Starting a router client is not enough to accept trigger delivery."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config_with_code_agent(tmp_path),
    )
    router_bot = MagicMock(spec=AgentBot, agent_name=ROUTER_AGENT_NAME)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router_bot})

    with (
        patch("mindroom.orchestration.entity_fleet.sync_forever_with_restart", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind,
    ):
        results = await orchestrator.fleet.start({ROUTER_AGENT_NAME})

    assert results.started_bots == [router_bot]
    mock_bind.assert_not_called()


def test_external_trigger_runtime_binds_router_with_live_readiness_gate(tmp_path: Path) -> None:
    """Trigger delivery runtime should bind the router with a live readiness gate."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config_with_code_agent(tmp_path),
    )

    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    router_bot.conversation_reader = object()

    target_bot = MagicMock(spec=AgentBot)
    target_bot.agent_name = "code"
    target_bot.running = False
    target_bot.client = object()
    install_bots(
        orchestrator,
        {
            ROUTER_AGENT_NAME: router_bot,
            "code": target_bot,
        },
    )

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        orchestrator._external_trigger_runtime.bind_if_ready(orchestrator.config, orchestrator.agent_bots)

    mock_bind.assert_called_once()
    assert mock_bind.call_args.kwargs["client"] is router_bot.client
    assert mock_bind.call_args.kwargs["conversation_reader"] is router_bot.conversation_reader
    assert callable(mock_bind.call_args.kwargs["is_trigger_snapshot_ready"])


@pytest.mark.asyncio
async def test_external_trigger_readiness_is_per_trigger_room(tmp_path: Path) -> None:
    """One ready trigger room should not make same-agent triggers in other rooms ready."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config_with_code_agent(tmp_path),
    )
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
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router_bot, "code": target_bot})

    async def get_joined_room_ids(client: object) -> list[str]:
        assert client in {router_client, target_client}
        return ["!campground:example.org"]

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms", side_effect=get_joined_room_ids):
        assert await orchestrator._external_trigger_runtime._is_ready(
            _trigger_snapshot(trigger_id="campground", room_id="!campground:example.org"),
            orchestrator.agent_bots,
        )
        assert not await orchestrator._external_trigger_runtime._is_ready(
            _trigger_snapshot(trigger_id="campground_other", room_id="!other:example.org"),
            orchestrator.agent_bots,
        )


@pytest.mark.asyncio
async def test_external_trigger_readiness_uses_matrix_joined_rooms(tmp_path: Path) -> None:
    """Trigger readiness should require both router and target joined rooms."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config_with_code_agent(tmp_path),
    )
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
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router_bot, "code": target_bot})

    async def get_joined_room_ids(client: object) -> list[str]:
        return {
            router_client: ["!campground:example.org"],
            target_client: ["!campground:example.org"],
        }[client]

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms", side_effect=get_joined_room_ids):
        assert await orchestrator._external_trigger_runtime._is_ready(
            _trigger_snapshot(),
            orchestrator.agent_bots,
        )


@pytest.mark.asyncio
async def test_external_trigger_binding_skips_when_embedded_api_disabled(tmp_path: Path) -> None:
    """No-API runs should not touch the bundled API app for trigger delivery."""
    config = _config_with_code_agent(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), api_enabled=False, config=config)
    router_bot = MagicMock(spec=AgentBot)
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router_bot})

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        orchestrator._external_trigger_runtime.bind_if_ready(orchestrator.config, orchestrator.agent_bots)

    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_startup_room_setup_binds_external_trigger_runtime_after_setup(tmp_path: Path) -> None:
    """Initial startup publishes trigger delivery only after room setup completes."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    orchestrator.running = True
    bot = MagicMock(spec=AgentBot)
    call_order: list[str] = []

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == [bot]
        call_order.append("setup")

    def bind_runtime(_config: Config | None, _bots: object) -> None:
        call_order.append("bind")

    with (
        patch.object(orchestrator._rooms, "setup", side_effect=setup_rooms),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready", side_effect=bind_runtime),
    ):
        try:
            await orchestrator._setup_startup_rooms_and_memberships([bot])
            slack_runtime = bound_slack_runtime(api_main.app)
            # Slack events reach the live bot mapping, so setup that runs again keeps the same runtime.
            assert slack_runtime is not None
            assert slack_runtime.bots is orchestrator.agent_bots
            await orchestrator._setup_startup_rooms_and_memberships([bot])
            assert bound_slack_runtime(api_main.app) is slack_runtime
        finally:
            unbind_integrations(api_main.app)

    assert call_order == ["setup", "bind", "setup", "bind"]


@pytest.mark.asyncio
async def test_trigger_support_only_reload_rebinds_external_trigger_runtime(tmp_path: Path) -> None:
    """Trigger-only reloads should publish a runtime bound to the new config generation."""
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(
        tmp_path,
        external_trigger_policy={"default_max_body_bytes": 4096, "max_body_bytes": 4096},
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=current_config)
    orchestrator.running = True
    install_bots(
        orchestrator,
        {
            ROUTER_AGENT_NAME: MagicMock(spec=AgentBot),
            "code": MagicMock(spec=AgentBot),
        },
    )
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        bots=orchestrator.agent_bots,
    )

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_apply_published_config_to_running_bots", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator._approval_recovery, "mark_startup_runtime_support_ready", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind_runtime,
        patch.object(orchestrator.agent_reply_memberships, "invalidate") as mock_invalidate_memberships,
    ):
        updated = await orchestrator._apply_config_update_plan(current_config, plan)

    assert updated is False
    mock_bind_runtime.assert_called_once_with(new_config, orchestrator.agent_bots)
    mock_invalidate_memberships.assert_not_called()


def test_log_mcp_degraded_entities_warns_per_failed_optional_server(tmp_path: Path) -> None:
    """Report only running entities as degraded by an unavailable optional MCP server."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=config)
    orchestrator._mcp_manager = _manager_with_failed_server(required=False)
    running_bot = MagicMock(spec=AgentBot)
    running_bot.running = True
    stopped_bot = MagicMock(spec=AgentBot)
    stopped_bot.running = False
    install_bots(orchestrator, {"code": running_bot, "dev_team": stopped_bot})

    with patch("mindroom.orchestrator.logger") as mock_logger:
        orchestrator._log_mcp_degraded_entities(config)

    mock_logger.warning.assert_called_once()
    kwargs = mock_logger.warning.call_args.kwargs
    assert kwargs["server_id"] == "demo"
    assert kwargs["degraded_entities"] == ["code"]


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_replaces_only_dependent_entities(tmp_path: Path) -> None:
    """Replace only the running MCP-dependent entities on the current config."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=config)
    orchestrator.running = True
    install_bots(orchestrator, {name: MagicMock(spec=AgentBot) for name in ("code", "dev_team", "plain")})

    with (
        patch.object(
            orchestrator.fleet,
            "replace",
            new=AsyncMock(return_value=EntityStartResults(retryable_entities=["code"])),
        ) as mock_replace,
        patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    mock_replace.assert_awaited_once_with({"code", "dev_team"})
    mock_clear_snapshot_cache.assert_called_once_with()


@pytest.mark.asyncio
async def test_unreferenced_mcp_catalog_change_skips_response_drain(tmp_path: Path) -> None:
    """A configured server with no dependents should not wait for unrelated responses."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                },
            },
            "agents": {
                "plain": {
                    "display_name": "Plain",
                    "role": "No MCP",
                },
            },
        },
        runtime_paths,
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config)
    orchestrator.running = True
    assert orchestrator.response_admission_gate.admit()

    try:
        with patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache:
            await asyncio.wait_for(orchestrator._handle_mcp_catalog_change("demo"), timeout=0.1)
    finally:
        orchestrator.response_admission_gate.release()

    mock_clear_snapshot_cache.assert_called_once_with()
    assert orchestrator.response_admission_gate.closed is False


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_sets_up_rooms_before_trigger_runtime_rebind(tmp_path: Path) -> None:
    """MCP restarts refresh rooms before publishing trigger runtime."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config)
    orchestrator.running = True
    install_bots(orchestrator, {"code": MagicMock(spec=AgentBot)})
    started_bot = MagicMock(spec=AgentBot)
    call_order: list[str] = []

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == [started_bot]
        call_order.append("setup")

    def bind_runtime(_config: Config | None, _bots: object) -> None:
        call_order.append("bind")

    with (
        patch.object(
            orchestrator.fleet,
            "replace",
            new=AsyncMock(return_value=EntityStartResults(started_bots=[started_bot])),
        ),
        patch.object(orchestrator._rooms, "setup", side_effect=setup_rooms),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready", side_effect=bind_runtime),
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    assert call_order == ["setup", "bind"]


@pytest.mark.asyncio
async def test_mcp_catalog_restart_replays_unfinished_startup_maintenance_with_live_bots(tmp_path: Path) -> None:
    """Startup room setup must not keep retrying a bot that a catalog restart replaced."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=config)
    orchestrator.running = True
    old_bot = MagicMock(spec=AgentBot, running=True)
    new_bot = MagicMock(spec=AgentBot, running=True)
    install_bots(orchestrator, {"code": old_bot})
    old_bot_stopped = asyncio.Event()
    old_setup_started = asyncio.Event()
    setup_calls: list[list[object]] = []

    async def setup_rooms(bots: list[object]) -> None:
        setup_calls.append(list(bots))
        if old_bot in bots:
            old_setup_started.set()
            await old_bot_stopped.wait()
            msg = "Matrix client is not ready for room lifecycle work"
            raise RuntimeError(msg)

    async def replace(*_args: object, **_kwargs: object) -> EntityStartResults:
        old_bot.running = False
        old_bot_stopped.set()
        swap_fleet_bot(orchestrator, "code", new_bot)
        return EntityStartResults(started_bots=[new_bot])

    with (
        patch.object(orchestrator.fleet, "replace", side_effect=replace),
        patch.object(orchestrator._rooms, "setup", side_effect=setup_rooms),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_bind_trigger_runtimes"),
        patch.object(
            orchestrator._approval_recovery,
            "mark_startup_runtime_support_ready",
            new=AsyncMock(),
        ) as mark_runtime_support_ready,
    ):
        orchestrator._startup_maintenance.start([old_bot], config)
        original_task = orchestrator._startup_maintenance.task
        try:
            await asyncio.wait_for(old_setup_started.wait(), timeout=1.0)
            await orchestrator._handle_mcp_catalog_change("demo")
            replayed_task = orchestrator._startup_maintenance.task
            assert original_task is not None
            assert original_task.cancelled()
            assert replayed_task is not None
            assert replayed_task is not original_task
            await asyncio.wait_for(replayed_task, timeout=1.0)
        finally:
            await orchestrator._startup_maintenance.cancel()

    assert setup_calls[-1] == [new_bot]
    mark_runtime_support_ready.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_name", "start_error"),
    [
        pytest.param(ROUTER_AGENT_NAME, RuntimeError("homeserver unavailable"), id="router-start-fails"),
        pytest.param("code", None, id="trigger-target-starts"),
    ],
)
async def test_restart_unbinds_external_trigger_runtime_before_stop_and_leaves_it_unbound(
    tmp_path: Path,
    entity_name: str,
    start_error: Exception | None,
) -> None:
    """Replacing the router or a trigger target fails trigger delivery closed until rooms are reconciled."""
    config = _config_with_code_agent(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=config)
    order: list[str] = []
    old_bot = MagicMock(spec=AgentBot)
    old_bot.stop = AsyncMock(side_effect=lambda **_kwargs: order.append("stop"))
    install_bots(orchestrator, {entity_name: old_bot})
    # The replacement stays stopped after its start, so its sync loop ends at once.
    new_bot = MagicMock(spec=AgentBot, agent_name=entity_name, running=False)
    new_bot.start = AsyncMock(side_effect=start_error)

    async def create(entity_names: set[str]) -> None:
        order.append("create")
        install_bots(orchestrator, dict.fromkeys(entity_names, new_bot))

    with (
        patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=lambda: order.append("unbind")),
        patch.object(orchestrator.fleet, "create", side_effect=create),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind_runtime,
    ):
        results = await orchestrator.fleet.replace({entity_name})
        await orchestrator.fleet.cancel_retries()

    assert order == ["unbind", "stop", "create"]
    assert results.retryable_entities == ([entity_name] if start_error else [])
    assert results.started_bots == ([] if start_error else [new_bot])
    mock_bind_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_post_update_rooms_does_not_bind_trigger_runtime(tmp_path: Path) -> None:
    """Room reconciliation should not bind trigger runtime before support services settle."""
    config = _config_with_code_agent(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=config)
    plan = ConfigUpdatePlan(
        new_config=config,
        restart={"code"},
        start=set(),
        remove=set(),
        mindroom_user_changed=False,
        room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    with (
        patch.object(orchestrator._rooms, "setup", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready") as mock_bind_runtime,
    ):
        await orchestrator._reconcile_post_update_rooms(plan, changed_entities=set())

    mock_bind_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_apply_config_update_plan_rebinds_trigger_runtime_after_support_services(
    tmp_path: Path,
) -> None:
    """Config reloads should finish with trigger runtime bound to the final API snapshot."""
    current_config = _config_with_code_agent(tmp_path)
    new_config = _config_with_code_agent(tmp_path, role="Write better code")
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=current_config)
    orchestrator.running = True
    router_bot = MagicMock(spec=AgentBot)
    router_bot.running = True
    router_bot.first_sync_complete = True
    router_bot.recover_pending_turn_journal_events = AsyncMock()
    code_bot = MagicMock(spec=AgentBot)
    code_bot.running = False
    code_bot.first_sync_complete = False
    install_bots(
        orchestrator,
        {
            ROUTER_AGENT_NAME: router_bot,
            "code": code_bot,
        },
    )
    plan = ConfigUpdatePlan(
        new_config=new_config,
        restart={"code"},
        start=set(),
        remove=set(),
        mindroom_user_changed=False,
        room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )
    call_order: list[str] = []

    async def reconcile_rooms(_plan: ConfigUpdatePlan, _changed_entities: set[str]) -> None:
        call_order.append("rooms")

    async def sync_support_services(_config: Config, *, previous_config: Config | None = None) -> None:
        assert previous_config is current_config
        call_order.append("support")

    def bind_runtime(_config: Config | None, _bots: object) -> None:
        assert _config is new_config
        call_order.append("bind")

    with (
        patch.object(orchestrator, "_prepare_accounts_for_config_update", new=AsyncMock()),
        patch.object(orchestrator._startup_maintenance, "cancel", new=AsyncMock(return_value=False)),
        patch.object(orchestrator.plugin_watch, "sync_roots"),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_apply_published_config_to_running_bots", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "unbind"),
        patch.object(orchestrator.fleet, "stop", new=AsyncMock()),
        patch.object(orchestrator.fleet, "start", new=AsyncMock(return_value=EntityStartResults())),
        patch.object(orchestrator, "_reconcile_post_update_rooms", side_effect=reconcile_rooms),
        patch.object(orchestrator, "_sync_runtime_support_services", side_effect=sync_support_services),
        patch.object(orchestrator._approval_recovery, "mark_startup_runtime_support_ready", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
        patch.object(orchestrator._external_trigger_runtime, "bind_if_ready", side_effect=bind_runtime),
    ):
        updated = await orchestrator._apply_config_update_plan(current_config, plan)

    assert updated is True
    assert call_order.count("bind") == 1
    assert call_order.index("rooms") < call_order.index("support") < call_order.index("bind")


@pytest.mark.asyncio
async def test_router_removal_unbinds_external_trigger_runtime_before_cleanup(tmp_path: Path) -> None:
    """Router removal should clear external trigger runtime before cleaning up the old client."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    order: list[str] = []
    external_trigger_runtime_bound = True

    async def cleanup() -> None:
        assert external_trigger_runtime_bound is False
        order.append("cleanup")

    router_bot = MagicMock(spec=AgentBot)
    router_bot.leave_rooms = AsyncMock(side_effect=cleanup)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router_bot})

    def unbind_external_trigger_runtime() -> None:
        nonlocal external_trigger_runtime_bound
        order.append("unbind")
        external_trigger_runtime_bound = False

    async def reconcile_before_cleanup(_removed_entities: set[str]) -> set[str]:
        order.append("reconcile")
        return set()

    with (
        patch.object(orchestrator._external_trigger_runtime, "unbind", side_effect=unbind_external_trigger_runtime),
        patch.object(
            orchestrator._approval_recovery,
            "reconcile_removed_entities",
            side_effect=reconcile_before_cleanup,
        ),
        patch.object(orchestrator, "event_journal_store", return_value=MagicMock(end_entity_replies=AsyncMock())),
    ):
        await orchestrator.fleet.remove({ROUTER_AGENT_NAME})

    assert ROUTER_AGENT_NAME not in orchestrator.agent_bots
    assert external_trigger_runtime_bound is False
    assert order == ["unbind", "reconcile", "cleanup"]


_TURN_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar("test_mcp_catalog_turn_owner", default=None)


@pytest.mark.asyncio
async def test_mcp_catalog_change_reported_during_a_turn_restarts_outside_the_turn_context(tmp_path: Path) -> None:
    """A tool call can report the change, and the restarted bots' sync tasks must not hold its turn's Agent."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    orchestrator.running = True
    observed: list[object | None] = []

    async def restart(_server_id: str) -> None:
        observed.append(_TURN_OWNER.get())

    token = _TURN_OWNER.set(object())
    try:
        with patch.object(orchestrator, "_handle_mcp_catalog_change", new=restart):
            await orchestrator._notify_mcp_catalog_change("demo")
    finally:
        _TURN_OWNER.reset(token)
    assert await wait_for_background_tasks(timeout=1, owner=orchestrator._mcp_catalog_change_task_owner)

    assert observed == [None]


@pytest.mark.asyncio
async def test_mcp_catalog_changes_merge_into_one_queued_restart(tmp_path: Path) -> None:
    """Changes reported while a restart waits are covered by it, and a change during the restart gets its own."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=_config(tmp_path))
    orchestrator.running = True
    install_bots(orchestrator, {"code": MagicMock(spec=AgentBot)})
    replace_calls = 0

    async def fake_replace(*_args: object, **_kwargs: object) -> EntityStartResults:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            await orchestrator._notify_mcp_catalog_change("demo")
        return EntityStartResults()

    with patch.object(orchestrator.fleet, "replace", side_effect=fake_replace):
        async with orchestrator._config_update_lock:
            for _ in range(20):
                await orchestrator._notify_mcp_catalog_change("demo")
                for _ in range(3):
                    await asyncio.sleep(0)
            assert replace_calls == 0
        assert await wait_for_background_tasks(timeout=5, owner=orchestrator._mcp_catalog_change_task_owner)

    assert replace_calls == 2
    assert orchestrator._pending_mcp_catalog_restarts == {}


@pytest.mark.asyncio
async def test_unreferenced_mcp_catalog_change_does_not_absorb_later_changes(tmp_path: Path) -> None:
    """A change that needs no restart must not leave the server marked as having one queued."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_config_with_code_agent(tmp_path),
    )
    orchestrator.running = True

    with patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache:
        for _ in range(2):
            await orchestrator._notify_mcp_catalog_change("demo")
            assert await wait_for_background_tasks(timeout=1, owner=orchestrator._mcp_catalog_change_task_owner)

    assert mock_clear_snapshot_cache.call_count == 2
    assert orchestrator._pending_mcp_catalog_restarts == {}


@pytest.mark.asyncio
async def test_handle_mcp_catalog_change_serializes_overlapping_restarts(tmp_path: Path) -> None:
    """Do not run overlapping restart cycles when multiple MCP servers hit the same entity."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config)
    orchestrator.running = True
    install_bots(orchestrator, {"code": MagicMock(spec=AgentBot)})

    first_restart_entered = asyncio.Event()
    allow_first_restart_to_finish = asyncio.Event()
    replace_calls: list[set[str]] = []

    async def fake_replace(entity_names: set[str]) -> EntityStartResults:
        replace_calls.append(set(entity_names))
        if len(replace_calls) == 1:
            first_restart_entered.set()
            await allow_first_restart_to_finish.wait()
        return EntityStartResults()

    with patch.object(orchestrator.fleet, "replace", side_effect=fake_replace):
        first_task = asyncio.create_task(orchestrator._handle_mcp_catalog_change("demo"))
        await first_restart_entered.wait()
        second_task = asyncio.create_task(orchestrator._handle_mcp_catalog_change("other"))
        await asyncio.sleep(0)
        assert len(replace_calls) == 1
        allow_first_restart_to_finish.set()
        await first_task
        await second_task

    assert replace_calls == [{"code"}, {"code"}]


@pytest.mark.asyncio
async def test_update_config_removes_and_stops_entities_before_publishing_and_syncing_mcp(tmp_path: Path) -> None:
    """Removed bots leave and restarted bots stop on the old config, before manager sync removes their servers.

    Removal runs first so the current router still delivers the removed entities' approval notices.
    """
    current_config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        api_enabled=False,
        config=current_config,
    )
    install_bots(
        orchestrator,
        {name: MagicMock(spec=AgentBot, running=False) for name in (ROUTER_AGENT_NAME, "code", "plain")},
    )
    updated_config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                },
            },
        },
        _runtime_paths(tmp_path),
    )
    persist_entity_accounts(current_config, orchestrator.runtime_paths)
    persist_entity_accounts(updated_config, orchestrator.runtime_paths)
    events: list[str] = []

    def record(event: str) -> None:
        events.append(f"{event}@{'new' if orchestrator.config is updated_config else 'old'}")

    async def remove(entity_names: set[str]) -> set[str]:
        record(f"remove:{','.join(sorted(entity_names))}")
        # Its approval settlement failed once, so the reload settles it again once the removal is published.
        return set(entity_names)

    async def stop(entity_names: set[str]) -> None:
        record(f"stop:{','.join(sorted(entity_names))}")

    async def reconcile_unavailable(entity_names: set[str]) -> None:
        record(f"settle:{','.join(sorted(entity_names))}")

    async def sync_mcp_manager(_config: Config) -> set[str]:
        record("mcp")
        return set()

    with (
        patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
        patch.object(orchestrator.fleet, "remove", side_effect=remove),
        patch.object(orchestrator.fleet, "stop", side_effect=stop),
        patch.object(
            orchestrator._approval_recovery,
            "reconcile_unavailable_entities",
            side_effect=reconcile_unavailable,
        ),
        patch.object(orchestrator, "_sync_mcp_manager", side_effect=sync_mcp_manager),
        patch.object(orchestrator.fleet, "start", new=AsyncMock(return_value=EntityStartResults())) as mock_start,
        patch.object(orchestrator, "_finalize_config_reload", new=AsyncMock()),
    ):
        assert await orchestrator.config_reload._update_config() is True

    assert events[:4] == ["remove:plain@old", "stop:code@old", "settle:plain@new", "mcp@new"]
    mock_start.assert_awaited_once_with({"code"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "plan_requires_drain"),
    [
        pytest.param({"defaults": {"enable_streaming": False}}, False, id="publishes-during-replies"),
        pytest.param(
            {"external_trigger_policy": {"default_max_body_bytes": 4096, "max_body_bytes": 4096}},
            True,
            id="drained",
        ),
    ],
)
async def test_reload_restarts_refreshed_mcp_dependents_inline_only_after_a_drain(
    tmp_path: Path,
    changes: dict[str, object],
    plan_requires_drain: bool,
) -> None:
    """A reload that may run during replies leaves catalog-refresh restarts to the drained MCP path."""
    current_config = _config(tmp_path)
    new_config = Config.validate_with_runtime(
        {**current_config.authored_model_dump(), **changes},
        _runtime_paths(tmp_path),
    )
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        api_enabled=False,
        config=current_config,
    )
    install_bots(
        orchestrator,
        {
            entity_name: MagicMock(spec=AgentBot, running=False)
            for entity_name in (ROUTER_AGENT_NAME, "code", "plain", "dev_team")
        },
    )
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        bots=orchestrator.agent_bots,
    )
    assert plan.requires_response_drain is plan_requires_drain
    assert not plan.restart
    code_bot = orchestrator.agent_bots["code"]
    plain_bot = orchestrator.agent_bots["plain"]

    async def stop(names: set[str]) -> None:
        for name in names:
            swap_fleet_bot(orchestrator, name, None)

    with (
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value={"demo"})),
        patch.object(orchestrator, "_notify_mcp_catalog_change", new=AsyncMock()) as mock_notify,
        patch.object(orchestrator.fleet, "stop", new=AsyncMock(side_effect=stop)) as mock_stop,
        patch.object(orchestrator.fleet, "create", new=AsyncMock()),
        patch.object(orchestrator.fleet, "start", new=AsyncMock(return_value=EntityStartResults())) as mock_start,
        patch.object(orchestrator, "_finalize_config_reload", new=AsyncMock()),
    ):
        await orchestrator._apply_config_update_plan(current_config, plan)

    # Kept bots adopt the new config; a dependent being replaced is stopped before it could.
    assert orchestrator.config is new_config
    plain_bot.apply_published_config.assert_called_once_with()
    assert code_bot.apply_published_config.called is not plan_requires_drain
    if plan_requires_drain:
        mock_notify.assert_not_awaited()
        assert mock_stop.await_args.args[0] == {"code", "dev_team"}
        mock_start.assert_awaited_once_with({"code", "dev_team"})
    else:
        mock_notify.assert_awaited_once_with("demo")
        assert mock_stop.await_args.args[0] == set()
        mock_start.assert_awaited_once_with(set())
