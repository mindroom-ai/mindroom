"""Tests for MCP-aware orchestrator reload planning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.config_updates import build_config_update_plan
from mindroom.orchestration.runtime import EntityStartResults
from mindroom.orchestrator import _MultiAgentOrchestrator

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config(tmp_path: Path, *, tool_name: str = "mcp_demo", command: str = "npx") -> Config:
    return Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": command,
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


@pytest.mark.asyncio
async def test_start_entities_marks_mcp_blocked_entities_retryable(tmp_path: Path) -> None:
    """Treat MCP discovery outages as retryable startup failures, not permanent disablement."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    orchestrator.agent_bots = {"code": MagicMock(spec=AgentBot)}

    with (
        patch.object(orchestrator, "_entities_blocked_by_failed_mcp_servers", side_effect=[{"code"}, {"code"}]),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())) as mock_sync,
        patch.object(orchestrator, "_try_start_bot_once", new=AsyncMock()) as mock_try_start,
    ):
        results = await orchestrator._start_entities_once(["code"], start_sync_tasks=False)

    assert results.retryable_entities == ["code"]
    assert results.permanently_failed_entities == []
    mock_sync.assert_awaited_once()
    mock_try_start.assert_not_awaited()


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
    orchestrator.agent_bots = {"code": MagicMock(spec=AgentBot)}
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
    call_order: list[str] = []

    async def fake_stop_entities(*_args: object, **_kwargs: object) -> None:
        call_order.append("stop")

    async def fake_sync_mcp_manager(_config: Config) -> set[str]:
        call_order.append("sync")
        return set()

    with (
        patch("mindroom.orchestrator.load_config", return_value=updated_config),
        patch("mindroom.orchestrator.stop_entities", new=AsyncMock(side_effect=fake_stop_entities)),
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(side_effect=fake_sync_mcp_manager)),
        patch.object(
            orchestrator,
            "_restart_changed_entities",
            new=AsyncMock(return_value=(set(), [], [])),
        ),
        patch.object(orchestrator, "_reconcile_post_update_rooms", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
    ):
        await orchestrator.update_config()

    assert call_order[:2] == ["stop", "sync"]
