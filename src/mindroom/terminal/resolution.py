"""The terminals of an agent's workspace, as a turn of the agent for one requester resolves them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agents import build_agent_toolkit
from mindroom.room_agent_access import toolkit_host
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.terminal.client import WorkerTerminals
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function, toolkit_worker_stand_ins
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def agent_terminals(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    *,
    create: bool,
) -> WorkerTerminals | None:
    """Return the terminals of ``agent_name``'s workspace as its turn for ``identity`` resolves it.

    None unless the agent's shell, built as that turn builds it, offers ``run_shell_command`` and runs in a worker.
    ``create`` makes the agent's workspace first, as a turn does. It blocks, building the agent's shell.
    """
    tools = config_plugins(config, runtime_paths).tools
    agent_tools = resolve_agent_tools(config, agent_name, tools)
    placement = tool_placement(runtime_paths, tools)
    entry = next((entry for entry in agent_tools.candidates(matrix_room_tools=True) if entry.name == "shell"), None)
    if entry is None or toolkit_host(agent_tools, "shell", runtime_paths) == "primary":
        return None
    shell = build_agent_toolkit(
        "shell",
        agent_tools=agent_tools,
        placement=placement,
        runtime_paths=runtime_paths,
        agent_runtime=resolve_agent_runtime(agent_name, config, runtime_paths, identity, create=create),
        tool_config_overrides=entry.tool_config_overrides,
        execution_identity=identity,
    )
    if shell is None or toolkit_function(shell, "run_shell_command") is None:
        return None
    stand_ins = toolkit_worker_stand_ins(shell)
    return None if stand_ins is None else WorkerTerminals(stand_ins)
