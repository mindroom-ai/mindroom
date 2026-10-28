"""What one agent, team, or the defaults scope gets from the config, resolved once with every inherited value filled in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig


@dataclass(frozen=True)
class AgentToolFacts:
    """What one agent's config decides about its toolkits, before a turn or the runtime decides the rest.

    ``authored`` are the agent's own and default tool entries in authored order, with merged settings.
    ``injected`` are toolkits its settings add without an entry: delegation, self-config, skill saving, and its graph.
    ``disabled`` are toolkits its settings turn off even when an entry names them.
    ``scope_incompatible`` maps a deferred entry to the expanded toolkits its execution scope cannot run.
    ``worker_tools`` is the resolved worker tool list, or None to follow the runtime's sandbox settings.
    """

    agent_name: str
    authored: tuple[EffectiveToolConfig, ...]
    injected: tuple[str, ...]
    disabled: frozenset[str]
    scope_incompatible: Mapping[str, tuple[str, ...]]
    worker_tools: tuple[str, ...] | None


def agent_tool_facts(config: Config, agent_name: str) -> AgentToolFacts:
    """Return what ``agent_name``'s config decides about its toolkits."""
    agent = config.get_agent(agent_name)
    uses_graph = config.agent_uses_knowledge_graph(agent_name)
    allow_self_config = (
        agent.allow_self_config if agent.allow_self_config is not None else config.defaults.allow_self_config
    )
    injected = [
        name
        for name, enabled in (
            ("delegate", bool(agent.delegate_to)),
            ("self_config", allow_self_config),
            # Like Hermes, an agent that learns skills can also save them in chat, and its skill review can only call
            # tools the agent's own request offered.
            ("skill_manage", agent.skill_learning.enabled),
            ("knowledge_graph", uses_graph),
        )
        if enabled
    ]
    disabled = {
        name
        for name, off in (
            ("memory", config.resolve_entity(agent_name).memory_backend == "none"),
            ("knowledge_graph", not uses_graph),
            ("delegate", not agent.delegate_to),
        )
        if off
    }
    authored = tuple(config.resolve_entity(agent_name).authored_tool_configs)
    execution_scope = config.resolve_entity(agent_name).execution_scope
    scope_incompatible = {
        entry.name: incompatible
        for entry in authored
        if entry.defer
        and (
            incompatible := tuple(
                unsupported_shared_only_integration_names(config.expand_tool_names([entry.name]), execution_scope),
            )
        )
    }
    worker_tools = agent.worker_tools if agent.worker_tools is not None else config.defaults.worker_tools
    return AgentToolFacts(
        agent_name=agent_name,
        authored=authored,
        injected=tuple(injected),
        disabled=frozenset(disabled),
        scope_incompatible=scope_incompatible,
        worker_tools=None if worker_tools is None else tuple(config.expand_tool_names(list(worker_tools))),
    )
