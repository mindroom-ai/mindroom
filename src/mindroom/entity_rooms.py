"""Configured Matrix rooms for agents, teams, and the router."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping


class _EntityRoomConfig(Protocol):
    """Entity config fields needed for room membership."""

    rooms: list[str]


class _EntityRoomsConfig(Protocol):
    """Root config fields needed for room membership."""

    agents: Mapping[str, _EntityRoomConfig]
    teams: Mapping[str, _EntityRoomConfig]

    def get_all_configured_rooms(self) -> set[str]:
        """Return all room references configured anywhere."""
        ...

    def get_external_trigger_rooms_for_entity(self, entity_name: str) -> list[str]:
        """Return enabled external trigger target rooms for one entity."""
        ...


def get_rooms_for_entity(entity_name: str, config: object) -> list[str]:
    """Return the room references an entity should join and treat as configured."""
    config_view = cast("_EntityRoomsConfig", config)
    if entity_name in config_view.teams:
        return _with_external_trigger_rooms(config_view.teams[entity_name].rooms, entity_name, config_view)

    if entity_name == ROUTER_AGENT_NAME:
        return list(config_view.get_all_configured_rooms())

    if entity_name in config_view.agents:
        return _with_external_trigger_rooms(config_view.agents[entity_name].rooms, entity_name, config_view)

    return []


def _with_external_trigger_rooms(
    base_rooms: list[str],
    entity_name: str,
    config: _EntityRoomsConfig,
) -> list[str]:
    """Append external trigger target rooms without duplicating explicit rooms."""
    return list(dict.fromkeys([*base_rooms, *config.get_external_trigger_rooms_for_entity(entity_name)]))
