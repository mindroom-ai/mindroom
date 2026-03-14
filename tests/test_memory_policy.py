"""Tests for memory scope and storage policy helpers."""

from __future__ import annotations

import pytest

from mindroom.config.main import Config
from mindroom.memory._policy import (
    agent_scope_user_id,
    get_allowed_memory_user_ids,
    get_team_ids_for_agent,
)
from tests.memory_test_support import MockTeamConfig


@pytest.fixture
def config() -> Config:
    """Load the default config for policy tests."""
    return Config.from_yaml()


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
