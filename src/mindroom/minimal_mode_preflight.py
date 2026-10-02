"""Minimal-mode eligibility shared by `!mode` and minimal subagents.

Every check reports a problem phrased as its fix, so an operator sees the
complete list at once instead of one requirement per attempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.agent_cli.shell_contract import SHELL_OPERATION_NAMES
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_approval import tool_may_require_approval
from mindroom.tool_system.catalog import ensure_tool_registry_loaded, get_tool_by_name
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_resolution import ResolvedAgentStorage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_SETUP_URL = "https://docs.mindroom.chat/tools/agent-cli/#deployment-requirements"


def _authored_shell(config: Config, agent_name: str) -> EffectiveToolConfig | None:
    return next(
        (entry for entry in config.resolve_entity(agent_name).authored_tool_configs if entry.name == "shell"),
        None,
    )


def _shell_problems(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    execution_identity: ToolExecutionIdentity,
    storage: ResolvedAgentStorage,
) -> list[str]:
    """Check the agent's effective shell functions without opening integrations or a worker."""
    shell = _authored_shell(config, agent_name)
    if shell is None:
        return [f"Add the `shell` tool to `{agent_name}`, because minimal mode reaches every tool through it."]
    worker_target = build_agent_toolkit_worker_target(
        storage.execution.execution_scope,
        agent_name,
        is_private=storage.execution.is_private,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
    )
    try:
        ensure_tool_registry_loaded(runtime_paths, config, load_plugin_tools=False)
        toolkit = get_tool_by_name(
            "shell",
            runtime_paths,
            runtime_config=config,
            tool_config_overrides=shell.tool_config_overrides,
            worker_target=worker_target,
            allowed_shared_services=(
                config.get_worker_grantable_credentials() if worker_target.worker_scope is not None else None
            ),
            disable_sandbox_proxy=True,
        )
    except (ImportError, ValueError):
        return [f"Fix the `shell` tool configuration of `{agent_name}` so it loads."]
    if not set(SHELL_OPERATION_NAMES).issubset(toolkit.get_async_functions()):
        return [f"Allow the run, check, and kill shell commands for `{agent_name}`."]
    return []


def minimal_mode_problems(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    execution_identity: ToolExecutionIdentity,
) -> list[str]:
    """Return every unmet agent and deployment requirement for one agent scope."""
    storage = resolve_agent_storage(agent_name, config, runtime_paths, execution_identity)
    problems = _shell_problems(config, runtime_paths, agent_name, execution_identity, storage)
    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=storage.state_root,
        use_state_storage_path=storage.execution.policy.private_workspace_enabled,
    )
    if workspace is None:
        problems.append(f"Give `{agent_name}` an agent workspace, for example with `memory_backend: file`.")
    return [*problems, *minimal_shell_problems(config, runtime_paths, agent_name)]


def minimal_subagent_candidates(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_names: Sequence[str],
    caller_identity: ToolExecutionIdentity | None,
) -> list[str]:
    """List agents to advertise as minimal subagents; a child that still cannot run fails with a recovery hint."""
    if (caller_identity is not None and caller_identity.channel != "matrix") or any(
        tool_may_require_approval(config, name) for name in SHELL_OPERATION_NAMES
    ):
        return []
    return [
        name
        for name in agent_names
        if name in config.agents
        and _authored_shell(config, name) is not None
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
