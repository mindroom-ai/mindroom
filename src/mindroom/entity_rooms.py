"""Configured Matrix rooms for agents, teams, and the router."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from mindroom.config.main import Config


def get_rooms_for_entity(entity_name: str, config: Config) -> list[str]:
    """Return the room references an entity should join and treat as configured."""
    if entity_name in config.teams:
        return list(config.teams[entity_name].rooms)

    if entity_name == ROUTER_AGENT_NAME:
        return list(config.get_all_configured_rooms())

    if entity_name in config.agents:
        return list(config.agents[entity_name].rooms)

    return []
