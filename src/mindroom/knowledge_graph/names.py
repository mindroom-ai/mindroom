"""How the knowledge graph names the agents and people MindRoom knows.

Graphiti merges two entities on its own only when their names are equal, so each agent and person gets one name: the
label of their lines in conversation episodes, the name the extraction instructions give with their other names, and the
name a lookup of any of those names finds.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import ROUTER_AGENT_NAME, ROUTER_DISPLAY_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.main import Config


@dataclass(frozen=True)
class KnownEntity:
    """An agent or person: the one name the graph uses for it and the other names it goes by."""

    name: str
    kind: Literal["agent", "person"]
    aliases: tuple[str, ...]


def _known(name: str, kind: Literal["agent", "person"], aliases: Iterable[str]) -> KnownEntity:
    """Return an entity listing each alias once in any letter case, and not its own name."""
    unique: dict[str, str] = {}
    for alias in aliases:
        unique.setdefault(alias.casefold(), alias)
    unique.pop(name.casefold(), None)
    return KnownEntity(name, kind, tuple(unique.values()))


def _localpart(matrix_id: str) -> str:
    return matrix_id.removeprefix("@").partition(":")[0]


def _canonical_name(display_name: str | None, fallback: str) -> str:
    """Return a display name as letters, digits, spaces, and ``.'-``, or ``fallback`` when no letter or digit is left.

    No colon remains, since Graphiti reads a speaker up to the first colon.
    """
    kept = ""
    for character in display_name or "":
        # A combining mark stays on the letter before it, so an accent stays and an emoji's variation selector goes.
        mark = unicodedata.category(character).startswith("M") and kept[-1:] not in ("", " ")
        kept += character if character.isalnum() or character in ".'-" or mark else " "
    name = " ".join(kept.split())
    return name if any(character.isalnum() for character in name) else fallback


@dataclass(frozen=True)
class EntityNames:
    """The names of the configured agents, teams, and router, and how people are named."""

    # By config key.
    agents: dict[str, KnownEntity]
    # The config key behind each prepared Matrix account.
    agent_keys: Mapping[str, str]

    def speaker(self, sender: str | None, display_name: str | None, own_agent: str) -> KnownEntity:
        """Return who said a message: the graph's own agent when ``sender`` is None, another agent, or a person.

        A person is named by ``display_name``, else by their Matrix ID's localpart.
        """
        if sender is None:
            return self.agents[own_agent]
        if (key := self.agent_keys.get(sender)) is not None:
            return self.agents[key]
        localpart = _localpart(sender)
        return _known(_canonical_name(display_name, localpart), "person", [sender, localpart])

    def known_entities(self, own_agent: str, speakers: Iterable[KnownEntity]) -> tuple[KnownEntity, ...]:
        """Return what an episode knows: the graph's own agent, then its speakers, one entity per name."""
        merged: dict[str, KnownEntity] = {}
        for entity in (self.agents[own_agent], *speakers):
            seen = merged.setdefault(entity.name.casefold(), entity)
            merged[entity.name.casefold()] = _known(seen.name, seen.kind, [*seen.aliases, *entity.aliases])
        return tuple(merged.values())


def entity_names(config: Config, agent_keys: Mapping[str, str]) -> EntityNames:
    """Return how ``config``'s agents and people are named; ``agent_keys`` maps prepared Matrix accounts to keys."""
    agents: dict[str, KnownEntity] = {}
    for key in [*config.agents, *config.teams, ROUTER_AGENT_NAME]:
        display_name = ROUTER_DISPLAY_NAME if key == ROUTER_AGENT_NAME else config.entity_display_name(key)
        ids = [matrix_id for matrix_id, owner in agent_keys.items() if owner == key]
        agents[key] = _known(_canonical_name(display_name, key), "agent", [key, *ids, *map(_localpart, ids)])
    return EntityNames(agents, agent_keys)
