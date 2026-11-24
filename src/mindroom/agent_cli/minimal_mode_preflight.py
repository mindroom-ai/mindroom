"""Minimal-mode eligibility shared by `!mode` and minimal subagents.

Every check reports a problem phrased as its fix, so an operator sees the
complete list at once instead of one requirement per attempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function_names
from mindroom.tool_system.catalog import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

_SETUP_URL = "https://docs.mindroom.chat/tools/agent-cli/#deployment-requirements"


def _effective_shell(config: Config, agent_name: str, tools: ToolRegistry) -> EffectiveToolConfig | None:
    """Return the agent's shell entry, whether it lists `shell` itself or a preset or another tool brings it in."""
    return next(
        (entry for entry in resolve_agent_tools(config, agent_name, tools).candidates() if entry.name == "shell"),
        None,
    )


def _shell_problems(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    worker_target: ResolvedWorkerTarget,
) -> list[str]:
    """Check the agent's effective shell functions without opening integrations or a worker."""
    try:
        tools = config_plugins(config, runtime_paths).tools
        shell = _effective_shell(config, agent_name, tools)
        if shell is None:
            return [f"Add the `shell` tool to `{agent_name}`, because minimal mode reaches every tool through it."]
        toolkit = get_tool_by_name(
            "shell",
            runtime_paths,
            tools=tools,
            runtime_config=config,
            tool_config_overrides=shell.tool_config_overrides,
            worker_target=worker_target,
            allowed_shared_services=(
                config.get_worker_grantable_credentials() if worker_target.worker_scope is not None else None
            ),
            runs_in_worker=False,
        )
    except (ImportError, ValueError):
        return [f"Fix the `shell` tool configuration of `{agent_name}` so it loads."]
    if SHELL_FUNCTION not in toolkit_function_names(toolkit):
        return [f"Allow the `{SHELL_FUNCTION}` shell function for `{agent_name}`."]
    return []


def minimal_mode_problems(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    execution_identity: ToolExecutionIdentity,
) -> list[str]:
    """Return every unmet agent and deployment requirement for one agent scope."""
    worker_target = resolve_agent_execution(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ).worker_target
    return [
        *_shell_problems(config, runtime_paths, agent_name, worker_target),
        *minimal_shell_problems(config, runtime_paths, agent_name),
    ]


def minimal_subagent_candidates(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_names: Sequence[str],
    caller_identity: ToolExecutionIdentity | None,
) -> list[str]:
    """List agents to advertise as minimal subagents; a child that still cannot run fails with a recovery hint."""
    if caller_identity is not None and caller_identity.channel != "matrix":
        return []
    if tool_may_require_approval(config, SHELL_FUNCTION):
        return []
    tools = config_plugins(config, runtime_paths).tools
    return [
        name
        for name in agent_names
        if name in config.agents
        and _effective_shell(config, name, tools) is not None
        and not minimal_shell_problems(config, runtime_paths, name)
    ]


def render_minimal_mode_problems(agent_name: str, problems: list[str], runtime_paths: RuntimePaths) -> str:
    """Render one operator-facing checklist with where deployment settings live."""
    lines = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"Minimal mode is not available for `{agent_name}` yet:\n{lines}\n"
        f"Environment settings belong in `{runtime_paths.env_path}`; restart MindRoom after changing them.\n"
        f"Setup guide: {_SETUP_URL}"
    )
