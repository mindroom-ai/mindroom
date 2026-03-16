"""Tests for memory scope and storage policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
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
    private_instance_state_root_path,
    resolve_worker_key,
    tool_execution_identity,
)
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config() -> Config:
    """Build the minimal config needed for policy tests."""
    return Config()


def test_get_team_ids_for_agent(config: Config) -> None:
    """Team scope IDs stay stable and include each matching team."""
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
    config.memory.team_reads_member_memory = False
    assert get_allowed_memory_user_ids(["general", "calculator"], config) == {"team_calculator+general"}

    config.memory.team_reads_member_memory = True
    assert get_allowed_memory_user_ids(["general", "calculator"], config) == {
        "agent_calculator",
        "agent_general",
        "team_calculator+general",
    }


def test_effective_storage_paths_for_mixed_private_team_prefers_private_roots(tmp_path: Path, config: Config) -> None:
    """Mixed teams should not write requester-local memory into shared agent roots."""
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
    worker_key = resolve_worker_key("user", identity, agent_name="general")
    expected_private_root = private_instance_state_root_path(
        tmp_path,
        worker_key=worker_key,
        agent_name="general",
    )

    with tool_execution_identity(identity):
        assert effective_storage_paths_for_context(["general", "calculator"], tmp_path, config) == [
            expected_private_root,
        ]


def test_storage_paths_for_scope_user_id_uses_member_root_for_mixed_private_team(
    tmp_path: Path,
    config: Config,
) -> None:
    """Member-scope access should still target each member's canonical storage root."""
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
    worker_key = resolve_worker_key("user", identity, agent_name="general")
    expected_private_root = private_instance_state_root_path(
        tmp_path,
        worker_key=worker_key,
        agent_name="general",
    )

    with tool_execution_identity(identity):
        assert storage_paths_for_scope_user_id("team_calculator+general", tmp_path, config) == [
            expected_private_root,
        ]
        assert storage_paths_for_scope_user_id("agent_calculator", tmp_path, config) == [
            agent_state_root_path(tmp_path, "calculator"),
        ]


def test_get_team_ids_for_agent_hides_private_team_ids_from_shared_members(config: Config) -> None:
    """Shared agents should not see requester-local mixed-team memory scopes."""
    config.agents = {
        "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="mind_data")),
        "calculator": AgentConfig(display_name="Calculator"),
    }
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    assert get_team_ids_for_agent("general", config) == ["team_calculator+general"]
    assert get_team_ids_for_agent("calculator", config) == []
