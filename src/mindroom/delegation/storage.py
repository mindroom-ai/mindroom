"""Frozen, path-free storage bindings for retained delegated runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config


def freeze_delegation_storage(config: Config, agent_names: Iterable[str]) -> dict[str, dict[str, object]]:
    """Retain only fields that locate session state and workspace audit records."""
    bindings: dict[str, dict[str, object]] = {}
    for name in agent_names:
        agent = config.get_agent(name)
        bindings[name] = {
            "display_name": name,
            "private": {"per": agent.private.per, "root": agent.private.root} if agent.private is not None else None,
            "worker_scope": None if agent.private is not None else agent.worker_scope or config.defaults.worker_scope,
        }
    return bindings


def delegation_storage_config(config: Config, bindings: dict[str, dict[str, object]]) -> Config:
    """Resolve retained storage after config removal without reconstructing capabilities."""
    agents = {**config.agents, **{name: AgentConfig.model_validate(binding) for name, binding in bindings.items()}}
    return config.model_copy(update={"agents": agents})
