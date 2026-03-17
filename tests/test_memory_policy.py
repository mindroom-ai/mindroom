"""Tests for memory scope and storage policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory._policy import (
    agent_scope_user_id,
    effective_storage_paths_for_context,
    get_allowed_memory_user_ids,
    get_team_ids_for_agent,
    storage_paths_for_scope_user_id,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Build the minimal config needed for policy tests."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    return bind_runtime_paths(Config(), runtime_paths)


def test_get_team_ids_for_agent(config: Config) -> None:
    """Team scope IDs stay stable and include each matching team."""
    config.agents = {
        "calculator": AgentConfig(display_name="Calculator"),
        "data_analyst": AgentConfig(display_name="Data Analyst"),
        "finance": AgentConfig(display_name="Finance"),
        "researcher": AgentConfig(display_name="Researcher"),
        "general": AgentConfig(display_name="General"),
        "assistant": AgentConfig(display_name="Assistant"),
    }
    config.teams = {
        "finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"]),
        "science_team": MockTeamConfig(agents=["calculator", "researcher"]),
        "other_team": MockTeamConfig(agents=["general", "assistant"]),
    }

    team_ids = get_team_ids_for_agent("calculator", config)
    assert len(team_ids) == 2
    assert "team_calculator+data_analyst+finance" in team_ids
    assert "team_calculator+researcher" in team_ids

    team_ids = get_team_ids_for_agent("general", config)
    assert len(team_ids) == 1
    assert "team_assistant+general" in team_ids

    assert get_team_ids_for_agent("unknown", config) == []


def test_scope_user_id_helpers() -> None:
    """Agent scope IDs are normalized consistently."""
    assert agent_scope_user_id("general") == "agent_general"


def test_get_allowed_memory_user_ids_for_team_context(config: Config) -> None:
    """Team callers only gain member scopes when that option is enabled."""
    config.agents = {
        "general": AgentConfig(display_name="General"),
        "calculator": AgentConfig(display_name="Calculator"),
    }
    config.memory.team_reads_member_memory = False
    assert get_allowed_memory_user_ids(["general", "calculator"], config) == {"team_calculator+general"}

    config.memory.team_reads_member_memory = True
    assert get_allowed_memory_user_ids(["general", "calculator"], config) == {
        "agent_calculator",
        "agent_general",
        "team_calculator+general",
    }


def test_effective_storage_paths_for_mixed_private_team_is_rejected(tmp_path: Path, config: Config) -> None:
    """Private agents are no longer supported in team memory contexts."""
    config.agents = {
        "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="mind_data")),
        "calculator": AgentConfig(display_name="Calculator"),
    }
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    with (
        tool_execution_identity(identity),
        pytest.raises(
            ValueError,
            match="private agents cannot participate in teams yet",
        ),
    ):
        effective_storage_paths_for_context(["general", "calculator"], tmp_path, config, runtime_paths_for(config))


def test_storage_paths_for_scope_user_id_rejects_mixed_private_team(
    tmp_path: Path,
    config: Config,
) -> None:
    """Team scope lookups should reject private team members."""
    config.agents = {
        "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="mind_data")),
        "calculator": AgentConfig(display_name="Calculator"),
    }
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    with (
        tool_execution_identity(identity),
        pytest.raises(
            ValueError,
            match="private agents cannot participate in teams yet",
        ),
    ):
        storage_paths_for_scope_user_id("team_calculator+general", tmp_path, config, runtime_paths_for(config))
    with tool_execution_identity(identity):
        assert storage_paths_for_scope_user_id("agent_calculator", tmp_path, config, runtime_paths_for(config)) == [
            agent_state_root_path(tmp_path, "calculator"),
        ]


def test_effective_storage_paths_for_team_rejects_transitive_private_delegate_target(
    tmp_path: Path,
    config: Config,
) -> None:
    """Team memory contexts should reject shared members that reach private agents via delegation."""
    config.agents = {
        "leader": AgentConfig(display_name="Leader", delegate_to=["mind"]),
        "helper": AgentConfig(display_name="Helper"),
        "mind": AgentConfig(display_name="Mind", private=AgentPrivateConfig(per="user", root="mind_data")),
    }
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="leader",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    with (
        tool_execution_identity(identity),
        pytest.raises(
            ValueError,
            match="reaches private agent 'mind' via delegation; private agents cannot participate in teams yet",
        ),
    ):
        effective_storage_paths_for_context(["leader", "helper"], tmp_path, config, runtime_paths_for(config))


def test_get_team_ids_for_agent_rejects_private_team_members(config: Config) -> None:
    """Configured private teams should be rejected if they reach memory policy helpers."""
    config.agents = {
        "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="mind_data")),
        "calculator": AgentConfig(display_name="Calculator"),
    }
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
        get_team_ids_for_agent("general", config)
