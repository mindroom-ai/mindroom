"""Tests for the public tool-runtime worker-target resolution API."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system.runtime_context import ToolRuntimeContext

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
    """A private user_agent-scoped agent resolves a requester-scoped target isolating itself."""
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


def test_authored_user_agent_scope_resolves_without_private_isolation(tmp_path: Path) -> None:
    """A shared agent with authored worker_scope=user_agent gets a requester-scoped key and no private set."""
    config = Config(agents={"scoped": {"display_name": "Scoped", "worker_scope": "user_agent"}})
    context = _context(config, "scoped", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope == "user_agent"
    assert target.routing_agent_name == "scoped"
    assert target.worker_key
    assert target.private_agent_names == frozenset()


def test_unscoped_agent_resolves_shared_target(tmp_path: Path) -> None:
    """An agent without any worker scope resolves an unscoped target."""
    config = Config(agents={"helper": {"display_name": "Helper"}})
    context = _context(config, "helper", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope is None
    assert target.routing_agent_name == "helper"
    assert target.worker_key is None
    assert target.private_agent_names is None


def test_team_dispatch_raises_a_purposeful_error(tmp_path: Path) -> None:
    """A team-named dispatch context fails loudly instead of resolving a wrong scope."""
    config = Config(
        agents={"code": {"display_name": "Code"}},
        teams={
            "super_team": {
                "display_name": "Super Team",
                "role": "Collaborative engineering assistant",
                "agents": ["code"],
                "mode": "collaborate",
            },
        },
    )
    context = _context(config, "super_team", tmp_path)

    with pytest.raises(ValueError, match="requires an agent dispatch"):
        context.resolve_worker_target()
