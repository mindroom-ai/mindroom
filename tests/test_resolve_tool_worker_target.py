"""Tests for the public tool-runtime worker-target resolution API."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from mindroom.config.main import Config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
)
from mindroom.tool_system.worker_routing import build_worker_target_from_runtime_env

if TYPE_CHECKING:
    from pathlib import Path


def _context(config: Config, agent_name: str, tmp_path: Path) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name=agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml"),
        event_cache=AsyncMock(),
        conversation_cache=AsyncMock(),
    )


def test_private_user_agent_scope_resolves_requester_scoped_target(tmp_path: Path) -> None:
    """A private user_agent-scoped agent resolves a requester-scoped target."""
    config = Config(
        agents={
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user_agent", "root": "workspace/mind_data"},
            },
        },
    )
    context = _context(config, "mind", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope == "user_agent"
    assert target.routing_agent_name == "mind"
    assert target.worker_key
    assert target.private_agent_names == frozenset({"mind"})


def test_resolution_matches_agent_construction_recipe(tmp_path: Path) -> None:
    """The method matches build_worker_target_from_runtime_env with agent-construction inputs."""
    config = Config(
        agents={
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user_agent", "root": "workspace/mind_data"},
            },
            "helper": {"display_name": "Helper"},
        },
    )
    for agent_name in ("mind", "helper"):
        context = _context(config, agent_name, tmp_path)
        worker_scope = config.agent_execution_scope(agent_name)
        agent_config = config.agents.get(agent_name)
        is_private = agent_config is not None and agent_config.private is not None
        if worker_scope == "user_agent":
            private_agent_names = frozenset({agent_name}) if is_private else frozenset()
        else:
            private_agent_names = None
        expected = build_worker_target_from_runtime_env(
            worker_scope,
            agent_name,
            execution_identity=build_execution_identity_from_runtime_context(context),
            runtime_paths=context.runtime_paths,
            private_agent_names=private_agent_names,
        )

        assert context.resolve_worker_target() == expected


def test_public_execution_scope_accessor_matches_internal(tmp_path: Path) -> None:
    """The public accessor delegates to the internal derivation."""
    del tmp_path
    config = Config(
        agents={
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user_agent", "root": "workspace/mind_data"},
            },
            "helper": {"display_name": "Helper"},
        },
    )
    for agent_name in ("mind", "helper"):
        assert config.agent_execution_scope(agent_name) == config._agent_execution_scope(agent_name)
