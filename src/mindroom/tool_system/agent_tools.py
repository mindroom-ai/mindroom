"""The toolkits one agent may use and where each one runs, resolved once from its config and the runtime."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.config.models import EffectiveToolConfig
from mindroom.config.resolution import agent_policy, agent_tool_facts, expanded_tool_names
from mindroom.tool_system.declarations import MATRIX_ROOM_RUNTIME_TOOL_NAMES
from mindroom.tool_system.metadata import validate_authored_tool_entry_overrides
from mindroom.tool_system.registry import builtin_tool_registry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config
    from mindroom.config.resolution import AgentToolFacts
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.sandbox_proxy import ToolPlacement
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def turn_room_flags(execution_identity: ToolExecutionIdentity | None) -> tuple[bool, bool]:
    """Return whether a turn under ``execution_identity`` keeps room-context toolkits and adds the Matrix room ones.

    A build without an identity keeps room-context toolkits, a turn without a room hides them, and only a Matrix room
    turn adds the Matrix room runtime toolkits.
    """
    if execution_identity is None:
        return True, False
    in_room = execution_identity.room_id is not None
    return in_room, in_room and execution_identity.channel == "matrix"


def _expanded_entry(entry: EffectiveToolConfig, tool_name: str) -> EffectiveToolConfig:
    return replace(
        entry,
        name=tool_name,
        tool_config_overrides=dict(entry.tool_config_overrides) if tool_name == entry.name else {},
        authored_name=entry.name,
    )


@dataclass(frozen=True)
class AgentTools:
    """The toolkits one agent may use and where each one runs.

    ``facts`` are what the agent's config decides: its authored entries, the toolkits its settings add, the ones they
    disable, and its worker tools. ``tools`` is the registry of what this runtime can build for the config, so it
    decides which authored toolkits are available. The turn decides the rest through ``candidates``: delegation depth,
    room, which deferred toolkits attach, and portable tool search. The runtime's ``ToolPlacement`` decides where each
    one runs.
    """

    config: Config
    facts: AgentToolFacts
    tools: ToolRegistry

    @property
    def agent_name(self) -> str:
        """The agent these toolkits belong to."""
        return self.facts.agent_name

    def runs_in_worker(self, tool_name: str, placement: ToolPlacement) -> bool:
        """Return whether this agent's calls of ``tool_name`` run in a worker under ``placement``.

        Toolkits MindRoom builds in this process without a registry factory, such as memory or delegation, never do.
        """
        metadata = self.tools.get(tool_name)
        if metadata is not None and metadata.factory is None:
            return False
        return placement.runs_in_worker(tool_name, self.facts.worker_tools)

    def workspace_in_worker(self, tool_names: Iterable[str], placement: ToolPlacement) -> bool:
        """Return whether any of ``tool_names`` that work on workspace paths runs in a worker, where attachments go."""
        return any(
            (metadata := self.tools.get(name)) is not None
            and metadata.consumes_workspace_paths
            and self.runs_in_worker(name, placement)
            for name in tool_names
        )

    def available(self, tool_name: str) -> bool:
        """Return whether this runtime can build ``tool_name`` for the agent's config.

        An authored tool whose plugin did not load, or one this runtime cannot run, is not offered, yet stays authored.
        """
        return self.tools.available(tool_name)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Every available toolkit the agent's entries name, presets and implied toolkits expanded, initial and deferred."""
        return tuple(name for name in self.facts.tool_names if self.available(name))

    def _owner_precedence(self, entry: EffectiveToolConfig) -> tuple[int, int, int]:
        directly_authored = entry.name == (entry.authored_name or entry.name)
        authored_from_individual_tool = not self.config.is_tool_preset(entry.authored_name or entry.name)
        return (int(directly_authored), int(authored_from_individual_tool), -entry.authored_order)

    def _available_authored(self) -> tuple[EffectiveToolConfig, ...]:
        """Return the authored entries that name a preset or a tool available here."""
        return tuple(
            entry
            for entry in self.facts.authored
            if self.config.is_tool_preset(entry.name) or self.available(entry.name)
        )

    def _visible_authored(self, attached: set[str]) -> list[EffectiveToolConfig]:
        visible: dict[str, EffectiveToolConfig] = {}
        hidden_deferred: dict[str, EffectiveToolConfig] = {}
        for authored in self._available_authored():
            owners = visible if not authored.defer or authored.name in attached else hidden_deferred
            for tool_name in expanded_tool_names(self.config, [authored.name], self.facts.disabled):
                if not self.available(tool_name):
                    continue
                expanded = _expanded_entry(authored, tool_name)
                current = owners.get(tool_name)
                if current is None or self._owner_precedence(expanded) > self._owner_precedence(current):
                    owners[tool_name] = expanded
        resolved: list[EffectiveToolConfig] = []
        for tool_name in self.tool_names:
            entry = visible.get(tool_name)
            if entry is None:
                continue
            hidden = hidden_deferred.get(tool_name)
            # An unattached deferred owner that outranks an eager one keeps the toolkit off until it attaches.
            if (
                hidden is not None
                and not entry.defer
                and self._owner_precedence(hidden) > self._owner_precedence(entry)
            ):
                continue
            resolved.append(entry)
        return resolved

    def candidates(
        self,
        *,
        depth: int = 0,
        room_tools: bool = True,
        matrix_room_tools: bool = False,
        deferred: Iterable[str] | None = None,
        portable_search: bool = False,
    ) -> tuple[EffectiveToolConfig, ...]:
        """Return the toolkits a turn may build, in the order it builds them.

        ``deferred`` limits which authored deferred toolkits attach, and None attaches all of them; deferred toolkits
        marked ``initial`` always attach. ``room_tools`` keeps the toolkits that need a room, ``matrix_room_tools`` adds
        the Matrix room runtime toolkits, and ``portable_search`` adds the deferred-tool search when anything defers.
        Which candidates a build actually offers also depends on filters and construction, which the build owns.
        """
        deferred_entries = [entry for entry in self._available_authored() if entry.defer]
        deferred_names = {entry.name for entry in deferred_entries}
        requested = deferred_names if deferred is None else set(deferred)
        attached = {
            entry.name
            for entry in deferred_entries
            if (entry.initial or entry.name in requested) and not self.facts.scope_incompatible.get(entry.name)
        }
        entries = [
            replace(
                entry,
                tool_config_overrides=validate_authored_tool_entry_overrides(
                    entry.name,
                    dict(entry.tool_config_overrides),
                    tool_metadata=self.tools.entries,
                ),
            )
            for entry in self._visible_authored(attached)
        ]
        injected = list(self.facts.injected)
        if portable_search and deferred_names:
            injected.append("dynamic_tools")
        if matrix_room_tools:
            entries = [entry for entry in entries if entry.name not in MATRIX_ROOM_RUNTIME_TOOL_NAMES]
            injected.extend(MATRIX_ROOM_RUNTIME_TOOL_NAMES)
        names = [entry.name for entry in entries]
        for tool_name in injected:
            if tool_name in names or (tool_name not in MATRIX_ROOM_RUNTIME_TOOL_NAMES and tool_name in deferred_names):
                continue
            entries.append(
                EffectiveToolConfig(
                    name=tool_name,
                    tool_config_overrides={},
                    authored_order=len(entries),
                    authored_name=tool_name,
                ),
            )
            names.append(tool_name)
        # Imported here to avoid the delegation -> personas -> agent tools import cycle.
        from mindroom.delegation.lifecycle import MAX_DELEGATION_DEPTH  # noqa: PLC0415

        return tuple(
            entry
            for entry in entries
            if (entry.name != "delegate" or depth < MAX_DELEGATION_DEPTH)
            and (room_tools or not self._needs_room(entry.name))
        )

    def toolkit_names(self, candidates: Iterable[EffectiveToolConfig]) -> list[str]:
        """Return the toolkits ``candidates`` name for the agent CLI and subagent tool lists; a preset has no functions."""
        return [entry.name for entry in candidates if not self.config.is_tool_preset(entry.name)]

    def _needs_room(self, tool_name: str) -> bool:
        metadata = self.tools.get(tool_name)
        return metadata is not None and metadata.requires_room_context


def room_turn_offers(config: Config, agent_name: str, tool_name: str) -> bool:
    """Return whether a Matrix room turn of ``agent_name`` offers the built-in ``tool_name``.

    Invalid tool settings offer nothing.
    """
    try:
        candidates = resolve_agent_tools(config, agent_name, builtin_tool_registry()).candidates(matrix_room_tools=True)
    except ValueError:
        return False
    return any(entry.name == tool_name for entry in candidates)


def resolve_agent_tools(config: Config, agent_name: str, tools: ToolRegistry) -> AgentTools:
    """Return the toolkits ``agent_name`` may use among the ``tools`` this runtime can build for ``config``."""
    return AgentTools(
        config=config,
        facts=agent_tool_facts(config, agent_name, policy=agent_policy(config, agent_name)),
        tools=tools,
    )
