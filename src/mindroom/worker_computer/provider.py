"""Resolve whether an agent's browser toolkit provides a worker computer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.placement import tool_placement
from mindroom.tool_system.plugins import config_plugins
from mindroom.worker_computer.sessions import ComputerError
from mindroom.workers.backend_kinds import worker_backend_kind

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["computer_browser_provider", "worker_computer_provider"]


def computer_browser_provider(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> str:
    """Return the one browser tool that gives this agent a worker computer, or raise the API's error."""
    if not worker_backend_kind(runtime_paths).dedicated:
        raise ComputerError(503, "Computer requires a dedicated Docker or Kubernetes worker backend.")
    providers = [
        provider
        for provider in ("browser", "browser_mcp")
        if config.agent_has_tool_at_execution_scope(agent_name, provider, "user_agent")
    ]
    if len(providers) != 1:
        raise ComputerError(409, "Computer requires exactly one browser provider and explicit user_agent worker scope.")
    provider = providers[0]

    # Match normal toolkit placement.
    tools = config_plugins(config, runtime_paths).tools
    if not resolve_agent_tools(config, agent_name, tools).runs_in_worker(
        provider,
        tool_placement(runtime_paths, tools),
    ):
        raise ComputerError(503, "Computer requires browser tools routed to the worker.")
    return provider


def worker_computer_provider(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the browser tool that gives this agent a worker computer, or None when it has none."""
    if agent_name not in config.agents or not runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV):
        return None
    try:
        return computer_browser_provider(agent_name, config, runtime_paths)
    except ComputerError:
        return None
