"""The one name each known agent and person has in the graph: as a speaker, in extraction instructions, and in lookups.

Graphiti merges two entities on its own only when their names are equal. A `knowledge_graph.aliases` entry names an
agent or person only through a Matrix ID or agent key it lists, never through a display name, which anyone can take.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import ROUTER_AGENT_NAME, ROUTER_DISPLAY_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.main import Config


@dataclass(frozen=True)
class KnownEntity:
    """An agent, person, or configured name: the one name the graph uses for it and the other names it goes by."""

    name: str
    # None for a `knowledge_graph.aliases` entry that links no agent or person.
    kind: Literal["agent", "person"] | None
    aliases: tuple[str, ...]


def _known(name: str, kind: Literal["agent", "person"] | None, aliases: Iterable[str]) -> KnownEntity:
    """Return an entity listing each alias once in any letter case, and not its own name."""
    unique = {name.casefold(): name}
    for alias in aliases:
        unique.setdefault(alias.casefold(), alias)
    return KnownEntity(name, kind, tuple(unique.values())[1:])


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
    """The names of the configured agents, teams, and router, of the `knowledge_graph.aliases` entries, and of people."""

    # By config key.
    agents: dict[str, KnownEntity]
    # By name; an entry that links an agent is that agent.
    entries: dict[str, KnownEntity]
    # The agent or entry each agent Matrix ID and each listed alias stands for.
    linked: dict[str, KnownEntity]

    def speaker(self, sender: str | None, display_name: str | None, own_agent: str) -> KnownEntity:
        """Return who said a message: the graph's own agent when ``sender`` is None, another agent, or a person.

        A person is named by the entry listing their Matrix ID, else by ``display_name``, else by their localpart.
        """
        if sender is None:
            return self.agents[own_agent]
        if (linked := self.linked.get(sender)) is not None:
            return linked
        localpart = _localpart(sender)
        return _known(_canonical_name(display_name, localpart), "person", [sender, localpart])

    def known_entities(self, own_agent: str, speakers: Iterable[KnownEntity]) -> tuple[KnownEntity, ...]:
        """Return what an episode knows: the graph's own agent, its speakers, and every entry, one entity per name.

        An alias that another listed entity is named, or that two of them go by, is left out, so no name means two.
        """
        merged: dict[str, KnownEntity] = {}
        for entity in (self.agents[own_agent], *speakers, *self.entries.values()):
            name = entity.name.casefold()
            seen = merged.get(name, entity)
            merged[name] = _known(seen.name, seen.kind or entity.kind, [*seen.aliases, *entity.aliases])
        listed = Counter(alias.casefold() for entity in merged.values() for alias in entity.aliases)
        kept = {alias for alias, count in listed.items() if count == 1 and alias not in merged}
        return tuple(
            _known(entity.name, entity.kind, [alias for alias in entity.aliases if alias.casefold() in kept])
            for entity in merged.values()
        )

    def canonical(self, name: str) -> str:
        """Return the one name of the entry or agent named ``name``, else of one also called it, in any letter case.

        A name wins over an alias, so an alias that another entity is named never finds the entry, as in the instructions.
        """
        wanted = name.casefold()
        entities = (*self.entries.values(), *self.agents.values())
        named = (entity.name for entity in entities if entity.name.casefold() == wanted)
        called = (entity.name for entity in entities if wanted in {alias.casefold() for alias in entity.aliases})
        return next(named, None) or next(called, name)


def entity_names(config: Config, agent_keys: Mapping[str, str]) -> EntityNames:
    """Return how ``config``'s agents, entries, and people are named; ``agent_keys`` maps agent Matrix IDs to keys."""
    configured = config.knowledge_graph.aliases
    entry_of = {alias: name for name, aliases in configured.items() for alias in aliases}
    entries = {
        name: _known(name, "person" if any(alias.startswith("@") for alias in aliases) else None, aliases)
        for name, aliases in configured.items()
    }
    agents: dict[str, KnownEntity] = {}
    for key in [*config.agents, *config.teams, ROUTER_AGENT_NAME]:
        display_name = ROUTER_DISPLAY_NAME if key == ROUTER_AGENT_NAME else config.entity_display_name(key)
        ids = [matrix_id for matrix_id, owner in agent_keys.items() if owner == key]
        # An entry listing the agent's key or Matrix ID names it, and then stands for the agent.
        entry = next((entries[entry_of[identity]] for identity in (key, *ids) if identity in entry_of), None)
        name, extra = (entry.name, entry.aliases) if entry else (_canonical_name(display_name, key), ())
        agents[key] = _known(name, "agent", [*extra, key, *ids, *map(_localpart, ids)])
        if entry is not None:
            entries[entry.name] = agents[key]
    linked = {alias: entries[name] for alias, name in entry_of.items()}
    return EntityNames(agents, entries, linked | {matrix_id: agents[key] for matrix_id, key in agent_keys.items()})
