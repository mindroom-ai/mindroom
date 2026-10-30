"""Tests for memory scope policy: team scope IDs, team store roots, and caller backends."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory._policy import caller_memory_backend, team_scope, team_scopes, team_user_id
from mindroom.memory_scope_ids import agent_scope_user_id
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.memory import MemoryBackend


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Build the minimal config needed for policy tests."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "calculator": AgentConfig(display_name="Calculator"),
            },
        ),
        runtime_paths,
    )


def test_scope_user_id_helpers() -> None:
    """Agent scope IDs are normalized consistently."""
    assert agent_scope_user_id("general") == "agent_general"


def test_team_user_id_is_the_same_for_every_member_order() -> None:
    """A team scope is named by its member set, so member order never splits it."""
    assert team_user_id(["general", "calculator"]) == "team_calculator+general"
    assert team_user_id(["calculator", "general"]) == "team_calculator+general"


def test_team_scope_directories_keep_earlier_names_within_the_name_limit(tmp_path: Path, config: Config) -> None:
    """A scope keeps the directory name its file copies had, and scopes differing late in a long member list stay apart."""
    runtime_paths = runtime_paths_for(config)
    members = [f"research_agent_{index:02d}" for index in range(1, 21)]
    long_roots = [
        team_scope(team_user_id(members), runtime_paths).root,
        team_scope(team_user_id([*members[:-1], "research_agent_99"]), runtime_paths).root,
    ]

    assert team_scope(team_user_id(["_alpha", "beta_"]), runtime_paths).root == (
        tmp_path / "team_memory" / "team__alpha+beta"
    )
    assert long_roots[0] != long_roots[1]
    for root in long_roots:
        assert root.parent == tmp_path / "team_memory"
        root.mkdir(parents=True)


def test_team_scopes_lists_each_member_set_of_the_agent_s_teams_once_whatever_their_backends(
    tmp_path: Path,
    config: Config,
) -> None:
    """An agent reads every configured team it belongs to, one scope per member set, in config order.

    Neither the members' backends nor their worker scopes partition a team's scope.
    """
    config.agents = {
        "calculator": AgentConfig(display_name="Calculator", memory_backend="file", worker_scope="user"),
        "data_analyst": AgentConfig(display_name="Data Analyst", memory_backend="mem0"),
        "finance": AgentConfig(display_name="Finance"),
        "researcher": AgentConfig(display_name="Researcher", memory_backend="none", worker_scope="user_agent"),
        "general": AgentConfig(display_name="General"),
        "assistant": AgentConfig(display_name="Assistant"),
    }
    config.teams = {
        "finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"]),
        "science_team": MockTeamConfig(agents=["calculator", "researcher"]),
        "science_team_again": MockTeamConfig(agents=["researcher", "calculator"]),
        "other_team": MockTeamConfig(agents=["general", "assistant"]),
    }
    runtime_paths = runtime_paths_for(config)

    def scope_ids(agent_name: str) -> list[str]:
        scopes = team_scopes(agent_name, config, runtime_paths)
        assert all(scope.root == tmp_path / "team_memory" / scope.user_id for scope in scopes)
        return [scope.user_id for scope in scopes]

    assert scope_ids("calculator") == ["team_calculator+data_analyst+finance", "team_calculator+researcher"]
    assert scope_ids("researcher") == ["team_calculator+researcher"]
    assert scope_ids("general") == ["team_assistant+general"]
    assert scope_ids("unknown") == []


@pytest.mark.parametrize(
    ("agents", "members", "rejection"),
    [
        pytest.param(
            {
                "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="mind")),
                "calculator": AgentConfig(display_name="Calculator"),
            },
            ["general", "calculator"],
            "includes private agent 'general'; private agents are only supported in explicit Matrix ad hoc teams "
            "with requester identity",
            id="private-member",
        ),
        pytest.param(
            {
                "calculator": AgentConfig(display_name="Calculator", delegate_to=["mind"]),
                "general": AgentConfig(display_name="General"),
                "mind": AgentConfig(display_name="Mind", private=AgentPrivateConfig(per="user", root="mind")),
            },
            ["calculator", "general"],
            "includes agent 'calculator' which reaches private agent 'mind' via delegation; private delegation is "
            "not supported for teams",
            id="private-delegate",
        ),
        pytest.param(
            {"calculator": AgentConfig(display_name="Calculator")},
            ["calculator", "ghost"],
            "references unknown agent 'ghost'",
            id="unknown-member",
        ),
    ],
)
def test_team_memory_rejects_the_members_team_validation_rejects(
    config: Config,
    agents: dict[str, AgentConfig],
    members: list[str],
    rejection: str,
) -> None:
    """A team with a private, privately delegating, or unknown member has no memory scope or backend."""
    config.agents = agents
    config.teams = {"mixed_team": MockTeamConfig(agents=members)}

    with pytest.raises(ValueError, match=re.escape(f"Team 'mixed_team' {rejection}")):
        team_scopes("calculator", config, runtime_paths_for(config))
    with pytest.raises(ValueError, match=re.escape(f"Team request {rejection}")):
        caller_memory_backend(config, members)


@pytest.mark.parametrize(
    ("general_backend", "calculator_backend", "team_backend"),
    [
        ("file", "file", "file"),
        ("none", "none", "none"),
        ("mem0", "mem0", "mem0"),
        ("file", "none", "mem0"),
        ("file", "mem0", "mem0"),
        ("none", "mem0", "mem0"),
    ],
)
def test_caller_memory_backend_of_a_team_is_file_or_none_only_when_every_member_agrees(
    config: Config,
    general_backend: MemoryBackend,
    calculator_backend: MemoryBackend,
    team_backend: MemoryBackend,
) -> None:
    """A team uses file memory when all members do, none when all do, and Mem0 otherwise; agents use their own."""
    config.memory.backend = "mem0"
    config.agents["general"].memory_backend = general_backend
    config.agents["calculator"].memory_backend = calculator_backend

    assert caller_memory_backend(config, "general") == general_backend
    assert caller_memory_backend(config, "calculator") == calculator_backend
    assert caller_memory_backend(config, ["general", "calculator"]) == team_backend


def test_caller_memory_backend_of_an_agent_without_its_own_backend_is_the_global_one(config: Config) -> None:
    """An agent that sets no memory backend uses the global one, alone or in a team."""
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "none"

    assert caller_memory_backend(config, "calculator") == "file"
    assert caller_memory_backend(config, ["general", "calculator"]) == "mem0"
