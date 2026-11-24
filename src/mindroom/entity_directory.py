"""The configured router, agents and teams by their current Matrix user ID, as plugins see them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.config.resolution import agent_policy, agent_tool_facts
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import prepared_entity_names_by_user_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class ManagedEntity:
    """The router, or one configured agent or team, behind one of MindRoom's own Matrix accounts.

    ``tool_names`` are the toolkits an agent's config gives it: those its tool entries and the defaults it takes name,
    a preset replaced by its toolkits, with the toolkits those imply, less those its settings turn off, then those its
    settings add, such as ``delegate``; the router and a team have none.
    """

    name: str
    kind: Literal["router", "agent", "team"]
    display_name: str
    tool_names: tuple[str, ...]


def entity_directory(config: Config, runtime_paths: RuntimePaths) -> Mapping[str, ManagedEntity]:
    """Return the router and each of ``config``'s agents and teams whose Matrix account exists, by its user ID."""
    return {
        user_id: _managed_entity(config, name)
        for user_id, name in prepared_entity_names_by_user_id(config, runtime_paths).items()
    }


def _managed_entity(config: Config, name: str) -> ManagedEntity:
    display_name = config.entity_display_name(name)
    if name == ROUTER_AGENT_NAME:
        return ManagedEntity(name, "router", display_name, ())
    if name in config.teams:
        return ManagedEntity(name, "team", display_name, ())
    facts = agent_tool_facts(config, name, policy=agent_policy(config, name))
    toolkits = dict.fromkeys(tool for tool in (*facts.tool_names, *facts.injected) if not config.is_tool_preset(tool))
    return ManagedEntity(name, "agent", display_name, tuple(toolkits))
