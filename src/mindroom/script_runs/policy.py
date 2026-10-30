"""Capability and approval policy for background script tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.script_runs.models import ScriptToolGrant
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import adapt_toolkit, toolkit_function_names
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = [
    "resolve_current_script_tool",
    "resolve_script_launch_grants",
    "resolve_script_launch_toolkit_names",
]


_SCRIPT_RESTRICTED_TOOLKITS = frozenset(
    {
        "browser",
        "script",
        "agent_manager",
        "claude_agent",
        "compact_context",
        "delegate",
        "dynamic_tools",
        "dynamic_workflow",
        "memory",
        "self_config",
        "skill_manage",
        # A script's calls rebuild the tool context without the turn's hook source, which Slack's write guard reads.
        "slack",
    },
)


def resolve_script_launch_grants(context: ToolRuntimeContext) -> tuple[ScriptToolGrant, ...]:
    """Resolve the stable registered function surface captured when a script launches."""
    tools = context.plugins.tools
    entries = _visible_script_tool_entries(context, context.config, tools)
    toolkits = _build_script_toolkits(context, context.config, tools, entries)
    return tuple(
        ScriptToolGrant(entry.name, function_name)
        for entry in entries
        if (toolkit := toolkits.get(entry.name)) is not None
        for function_name in _visible_function_names(context, entry, toolkit)
    )


def resolve_script_launch_toolkit_names(context: ToolRuntimeContext) -> frozenset[str]:
    """Resolve eligible toolkit names independently of their current function catalogs."""
    tools = context.plugins.tools
    return frozenset(entry.name for entry in _visible_script_tool_entries(context, context.config, tools))


def resolve_current_script_tool(
    context: ToolRuntimeContext,
    grant: ScriptToolGrant,
    *,
    rejected_toolkit_cleanup: Callable[[AnyToolkit], None] | None = None,
) -> AnyToolkit | None:
    """Build one requested live toolkit only when its granted function remains visible."""
    config = context.current_config
    if context.agent_name not in config.agents:
        return None
    tools = config_plugins(config, context.runtime_paths).tools
    tool_entry = next(
        (entry for entry in _visible_script_tool_entries(context, config, tools) if entry.name == grant.toolkit_name),
        None,
    )
    if tool_entry is None:
        return None
    toolkit = _build_script_toolkits(context, config, tools, (tool_entry,)).get(grant.toolkit_name)
    if toolkit is None:
        return None
    if grant.function_name not in _visible_function_names(context, tool_entry, toolkit):
        if rejected_toolkit_cleanup is not None:
            rejected_toolkit_cleanup(toolkit)
        return None
    return toolkit


def _visible_script_tool_entries(
    context: ToolRuntimeContext,
    config: Config,
    tools: ToolRegistry,
) -> tuple[EffectiveToolConfig, ...]:
    if context.agent_name not in config.agents:
        msg = f"Background scripts require a configured agent owner; {context.agent_name!r} is not an agent."
        raise ValueError(msg)
    return tuple(
        entry
        for entry in resolve_agent_tools(config, context.agent_name, tools).candidates()
        if entry.name in tools and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
    )


def _build_script_toolkits(
    context: ToolRuntimeContext,
    config: Config,
    tools: ToolRegistry,
    tool_entries: Sequence[EffectiveToolConfig],
) -> dict[str, AnyToolkit]:
    # Imported lazily to keep script-run state imports independent of the agent/model graph.
    from mindroom.agents import build_agent_toolkit  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    agent_tools = resolve_agent_tools(config, context.agent_name, tools)
    placement = tool_placement(context.runtime_paths, tools)
    workspace_in_worker = agent_tools.workspace_in_worker([entry.name for entry in tool_entries], placement)
    execution_identity = context.execution_identity
    agent_runtime = resolve_agent_runtime(
        context.agent_name,
        config,
        context.runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    toolkits: dict[str, AnyToolkit] = {}
    for tool_entry in tool_entries:
        toolkit = build_agent_toolkit(
            tool_entry.name,
            agent_tools=agent_tools,
            placement=placement,
            runtime_paths=context.runtime_paths,
            workspace_in_worker=workspace_in_worker,
            agent_runtime=agent_runtime,
            tool_config_overrides=tool_entry.tool_config_overrides,
            execution_identity=execution_identity,
        )
        if toolkit is not None:
            toolkits[tool_entry.name] = toolkit
    return toolkits


def _visible_function_names(
    context: ToolRuntimeContext,
    tool_entry: EffectiveToolConfig,
    toolkit: AnyToolkit,
) -> tuple[str, ...]:
    # One function per name, the one an agent's run offers.
    if context.tool_function_filter is None:
        return toolkit_function_names(toolkit)
    # Each function carries the toolkit names the agent's build gives it: the authored toolkit it is offered from,
    # which for an implied or preset tool is the tool that brings it in, and the registered tool it comes from, so the
    # run's filter judges the same function the agent sees.
    functions = adapt_toolkit(
        toolkit,
        key=tool_entry.authored_name or tool_entry.name,
        registered=tool_entry.name,
    ).functions
    return tuple(function.name for function in functions if context.tool_function_filter(function))
