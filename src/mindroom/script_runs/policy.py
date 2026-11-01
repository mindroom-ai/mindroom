"""Capability and approval policy for background script tool calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.script_runs.models import ScriptToolGrant
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import adapt_toolkit, toolkit_function
from mindroom.tool_system.sandbox_proxy import tool_placement

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

__all__ = [
    "CurrentScriptTool",
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
        "contacts",
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
    entries = _visible_script_tool_entries(context, context.config)
    toolkits = _build_script_toolkits(context, context.config, entries)
    return tuple(
        ScriptToolGrant(entry.name, function_name)
        for entry in entries
        if (toolkit := toolkits.get(entry.name)) is not None
        for function_name in _visible_function_names(context, entry, toolkit)
    )


def resolve_script_launch_toolkit_names(context: ToolRuntimeContext) -> frozenset[str]:
    """Resolve eligible toolkit names independently of their current function catalogs."""
    return frozenset(entry.name for entry in _visible_script_tool_entries(context, context.config))


@dataclass(frozen=True, slots=True)
class CurrentScriptTool:
    """The live toolkit a script's call runs from and its granted function, adapted alone as an agent is offered it."""

    toolkit: AnyToolkit
    function: ToolFunction


def resolve_current_script_tool(
    context: ToolRuntimeContext,
    grant: ScriptToolGrant,
    *,
    rejected_toolkit_cleanup: Callable[[AnyToolkit], None] | None = None,
) -> CurrentScriptTool | None:
    """Build one requested live toolkit only when its granted function remains visible, adapting that function alone."""
    config = context.current_config
    if context.agent_name not in config.agents:
        return None
    tool_entry = next(
        (entry for entry in _visible_script_tool_entries(context, config) if entry.name == grant.toolkit_name),
        None,
    )
    if tool_entry is None:
        return None
    toolkit = _build_script_toolkits(context, config, (tool_entry,)).get(grant.toolkit_name)
    if toolkit is None:
        return None
    function = toolkit_function(toolkit, grant.function_name)
    if function is not None:
        # The function as the agent's build offers it, so the grant rule judges what the agent sees.
        function = replace(
            function,
            toolkit=tool_entry.authored_name or tool_entry.name,
            registered_toolkit=tool_entry.name,
        )
    if function is None or not _grantable(context, function):
        if rejected_toolkit_cleanup is not None:
            rejected_toolkit_cleanup(toolkit)
        return None
    return CurrentScriptTool(toolkit, function)


def _visible_script_tool_entries(context: ToolRuntimeContext, config: Config) -> tuple[EffectiveToolConfig, ...]:
    if context.agent_name not in config.agents:
        msg = f"Background scripts require a configured agent owner; {context.agent_name!r} is not an agent."
        raise ValueError(msg)
    # The registry the call's tool hooks come from, so its toolkits and hooks share one generation.
    tools = context.plugins.tools
    return tuple(
        entry
        for entry in resolve_agent_tools(config, context.agent_name, tools).candidates()
        if entry.name in tools and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
    )


def _build_script_toolkits(
    context: ToolRuntimeContext,
    config: Config,
    tool_entries: Sequence[EffectiveToolConfig],
) -> dict[str, AnyToolkit]:
    # Imported lazily to keep script-run state imports independent of the agent/model graph.
    from mindroom.agents import build_agent_toolkit  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    tools = context.plugins.tools
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
    # One function per name, the one an agent's run offers. Each function carries the toolkit names the agent's build
    # gives it: the authored toolkit it is offered from, which for an implied or preset tool is the tool that brings it
    # in, and the registered tool it comes from, so the run's filter judges the same function the agent sees.
    functions = adapt_toolkit(
        toolkit,
        key=tool_entry.authored_name or tool_entry.name,
        registered=tool_entry.name,
    ).functions
    return tuple(function.name for function in functions if _grantable(context, function))


def _grantable(context: ToolRuntimeContext, function: ToolFunction) -> bool:
    """Whether a script may call ``function``: the run's filter keeps it, and it cannot stop the run.

    A script's call answers the script, never a run, so it cannot make one that stops the run.
    """
    run_filter = context.tool_function_filter
    return not function.controls_run and (run_filter is None or run_filter(function))
