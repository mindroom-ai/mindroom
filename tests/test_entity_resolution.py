"""Tests for runtime-derived entity resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.entity_resolution import configured_bot_usernames_for_room
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path


def test_configured_bot_usernames_for_room_includes_agents_teams_and_router(tmp_path: Path) -> None:
    """Room membership resolution returns agent, team, and router bot usernames."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General agent", rooms=["!room:server"]),
                "other": AgentConfig(display_name="Other", role="Other agent", rooms=["!other:server"]),
            },
            teams={
                "team": TeamConfig(
                    display_name="Team",
                    role="Team role",
                    agents=["general"],
                    rooms=["!room:server"],
                ),
            },
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )

    usernames = configured_bot_usernames_for_room(config, "!room:server", runtime_paths_for(config))

    assert usernames == {
        f"mindroom_{ROUTER_AGENT_NAME}",
        "mindroom_general",
        "mindroom_team",
    }
