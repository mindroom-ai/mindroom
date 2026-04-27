"""Runtime-derived entity resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.identity import agent_username_localpart
from mindroom.matrix.rooms import resolve_room_aliases

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def configured_bot_usernames_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return bot username localparts configured for one Matrix room."""
    configured_bots: set[str] = set()

    for agent_name, agent_config in config.agents.items():
        resolved_rooms = set(resolve_room_aliases(agent_config.rooms, runtime_paths))
        if room_id in resolved_rooms:
            configured_bots.add(agent_username_localpart(agent_name, runtime_paths))

    for team_name, team_config in config.teams.items():
        resolved_rooms = set(resolve_room_aliases(team_config.rooms, runtime_paths))
        if room_id in resolved_rooms:
            configured_bots.add(agent_username_localpart(team_name, runtime_paths))

    if configured_bots:
        configured_bots.add(agent_username_localpart(ROUTER_AGENT_NAME, runtime_paths))

    return configured_bots
