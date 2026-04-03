"""Tests for MCP-aware orchestrator reload planning."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.config_updates import build_config_update_plan
from mindroom.orchestration.runtime import EntityStartResults
from mindroom.orchestrator import MultiAgentOrchestrator

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
    orchestrator = MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
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
    orchestrator = MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
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
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    changed_entities = mock_create_and_start.await_args.args[0]
    assert changed_entities == {"code", "dev_team"}
    assert mock_stop_entities.await_args.args[0] == {"code", "dev_team"}
    assert mock_create_and_start.await_args.kwargs["start_sync_tasks"] is True
    assert {args.args[0] for args in mock_cancel.await_args_list} == {"code", "dev_team"}
    mock_schedule_retry.assert_awaited_once_with("code")
