"""Capability and approval policy for background script tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.script_runs.models import ScriptToolGrant
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.agno_toolkits import Toolkit
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
        "claude_agent",
        "compact_context",
        "delegate",
        "dynamic_tools",
        "dynamic_workflow",
        "memory",
        "self_config",
        "skill_manage",
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


def resolve_current_script_tool(
    context: ToolRuntimeContext,
    grant: ScriptToolGrant,
    *,
    rejected_toolkit_cleanup: Callable[[Toolkit], None] | None = None,
) -> Toolkit | None:
    """Build one requested live toolkit only when its granted function remains visible."""
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
    if grant.function_name not in _visible_function_names(context, tool_entry, toolkit):
        if rejected_toolkit_cleanup is not None:
            rejected_toolkit_cleanup(toolkit)
        return None
    return toolkit


def _visible_script_tool_entries(
    context: ToolRuntimeContext,
    config: Config,
) -> tuple[EffectiveToolConfig, ...]:
    if context.agent_name not in config.agents:
        msg = f"Background scripts require a configured agent owner; {context.agent_name!r} is not an agent."
        raise ValueError(msg)
    ensure_tool_registry_loaded(context.runtime_paths, config)
    return tuple(
        entry
        for entry in visible_tool_surface(
            agent_name=context.agent_name,
            config=config,
        ).runtime_tool_configs
        if entry.name in TOOL_METADATA and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
    )


def _build_script_toolkits(
    context: ToolRuntimeContext,
    config: Config,
    tool_entries: Sequence[EffectiveToolConfig],
) -> dict[str, Toolkit]:
    # Imported lazily to keep script-run state imports independent of the agent/model graph.
    from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    entity_view = config.resolve_entity(context.agent_name)
    execution_identity = build_execution_identity_from_runtime_context(context)
    agent_runtime = resolve_agent_runtime(
        context.agent_name,
        config,
        context.runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    worker_tools = resolve_runtime_worker_tools(
        context.agent_name,
        config,
        context.runtime_paths,
        [entry.name for entry in tool_entries],
        tool_registry_preloaded=True,
    )
    toolkits: dict[str, Toolkit] = {}
    for tool_entry in tool_entries:
        toolkit = build_agent_toolkit(
            tool_entry.name,
            agent_name=context.agent_name,
            config=config,
            runtime_paths=context.runtime_paths,
            worker_tools=worker_tools,
            runtime_overrides=entity_view.tool_runtime_overrides(tool_entry.name),
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
    toolkit: Toolkit,
) -> tuple[str, ...]:
    # One function per name, the one an agent's run offers.
    if context.tool_function_filter is None:
        return tuple(toolkit.get_async_functions())
    # Each function names its authored toolkit, the one an agent's build offers it from, which for an implied or
    # preset tool is the tool that brings it in.
    functions = adapt_toolkit(toolkit, key=tool_entry.authored_name or tool_entry.name).functions
    return tuple(function.name for function in functions if context.tool_function_filter(function))
