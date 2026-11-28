"""Determine which agents use the egress broker and whose credentials they use."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mindroom.egress_broker.env import EGRESS_TOOL_NAME
from mindroom.runtime_env_policy import EGRESS_BROKER_ENV_BY_KEY
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.placement import tool_placement
from mindroom.tool_system.plugins import config_plugins
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.agent_tools import AgentTools

__all__ = ["EgressAgentScope", "broker_serves_primary_shell", "egress_agent_scope"]

type EgressAgentScope = Literal["user", "user_agent", "shared", "unscoped"]


def egress_agent_scope(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> EgressAgentScope | None:
    """Return whose store an agent's egress services and keys live in, or None when it does not use the broker.

    An agent opts in by listing the `egress_credentials` tool, and its brokered traffic only comes from a command tool
    that gets the broker environment: `shell`. Both must be among its resolved tools, presets and defaults included.
    The sandbox proxy adds the broker environment to worker-routed calls and the tool-call policy to shell calls
    in the primary. The store is one `user_agent` agent's own per requester, every `user` agent's
    of a requester, everyone's on a `shared` agent, or the global one all agents without a worker scope share
    (`unscoped`).
    The sandbox proxy, the tool-call policy, and both egress APIs decide here which agents use the broker.
    """
    if agent_name not in config.agents:
        return None
    # While the broker is off, the egress tool is unavailable, so no agent uses the broker.
    if not _uses_broker(_agent_tools(config, agent_name, runtime_paths), runtime_paths):
        return None
    execution_scope = config.resolve_agent(agent_name).policy.effective_execution_scope
    return "unscoped" if execution_scope is None else execution_scope


def broker_serves_primary_shell(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether an agent that uses the broker runs its `shell` in the primary.

    The tool-call policy then gives each of its shell calls the broker environment; a worker-routed shell call gets it
    from the sandbox proxy instead.
    """
    if agent_name not in config.agents:
        return False
    return _primary_shell_uses_broker(_agent_tools(config, agent_name, runtime_paths), runtime_paths)


def _primary_shell_uses_broker(agent_tools: AgentTools, runtime_paths: RuntimePaths) -> bool:
    tool_names = agent_tools.tool_names
    return (
        EGRESS_TOOL_NAME in tool_names
        and "shell" in tool_names
        and not _shell_runs_in_a_worker(agent_tools, runtime_paths)
    )


def _agent_tools(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> AgentTools:
    return resolve_agent_tools(config, agent_name, config_plugins(config, runtime_paths).tools)


def _uses_broker(agent_tools: AgentTools, runtime_paths: RuntimePaths) -> bool:
    """Return whether the agent lists `egress_credentials` and a `shell` that can reach the broker.

    The default broker, without a port, has no worker-facing URL, so only a `shell` in the primary uses it.
    """
    if not runtime_paths.env_value(EGRESS_BROKER_ENV_BY_KEY["port"], default="").strip():
        return _primary_shell_uses_broker(agent_tools, runtime_paths)
    return EGRESS_TOOL_NAME in agent_tools.tool_names and "shell" in agent_tools.tool_names


def _shell_runs_in_a_worker(agent_tools: AgentTools, runtime_paths: RuntimePaths) -> bool:
    """Return whether the agent's shell runs in a worker."""
    try:
        placement = tool_placement(runtime_paths, agent_tools.tools)
        return agent_tools.runs_in_worker("shell", placement)
    except WorkerBackendError:
        # An unreadable worker backend never runs the tool in the primary, so the agent stays listed and each page
        # degrades per service instead of failing.
        return True
