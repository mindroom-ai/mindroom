"""Capability and approval policy for background script tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.script_runs.models import ScriptToolGrant
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context

if TYPE_CHECKING:
    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = ["effective_script_grants", "resolve_current_script_grants", "resolve_script_launch_grants"]


_SCRIPT_RESTRICTED_TOOLKITS = frozenset(
    {"script", "compact_context", "delegate", "dynamic_tools", "dynamic_workflow", "memory", "self_config"},
)


def effective_script_grants(
    launch_grants: tuple[ScriptToolGrant, ...],
    current_grants: frozenset[ScriptToolGrant],
) -> frozenset[ScriptToolGrant]:
    """Return authority present in both the immutable launch snapshot and live surface."""
    return frozenset(launch_grants) & current_grants


def resolve_script_launch_grants(
    context: ToolRuntimeContext,
) -> tuple[ScriptToolGrant, ...]:
    """Resolve the stable registered function surface captured when a script launches."""
    return _resolve_script_grants(context, context.config)


def resolve_current_script_grants(
    context: ToolRuntimeContext,
) -> frozenset[ScriptToolGrant]:
    """Resolve the owning agent's registered function surface from live configuration."""
    config = context.current_config
    if context.agent_name not in config.agents:
        return frozenset()
    return frozenset(_resolve_script_grants(context, config))


def _resolve_script_grants(context: ToolRuntimeContext, config: Config) -> tuple[ScriptToolGrant, ...]:
    if context.agent_name not in config.agents:
        msg = f"Background scripts require a configured agent owner; {context.agent_name!r} is not an agent."
        raise ValueError(msg)

    ensure_tool_registry_loaded(context.runtime_paths, config)
    entity_view = config.resolve_entity(context.agent_name)
    tool_configs = {
        entry.name: entry
        for entry in entity_view.tool_configs
        if entry.name in TOOL_METADATA and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
    }
    tool_names = list(tool_configs)

    # Imported lazily to keep script-run state imports independent of the agent/model graph.
    from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

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
        tool_names,
        tool_registry_preloaded=True,
    )
    toolkits: list[tuple[str, Toolkit]] = []
    for toolkit_name, tool_entry in tool_configs.items():
        toolkit = build_agent_toolkit(
            toolkit_name,
            agent_name=context.agent_name,
            config=config,
            runtime_paths=context.runtime_paths,
            worker_tools=worker_tools,
            runtime_overrides=entity_view.tool_runtime_overrides(toolkit_name),
            agent_runtime=agent_runtime,
            tool_config_overrides=tool_entry.tool_config_overrides,
            execution_identity=execution_identity,
            session_id=context.session_id,
        )
        if toolkit is not None:
            toolkits.append((toolkit_name, toolkit))

    grants: list[ScriptToolGrant] = []
    for toolkit_name, toolkit in toolkits:
        if context.tool_function_filter is None:
            function_names = dict.fromkeys((*toolkit.functions, *toolkit.async_functions))
        else:
            visible_sync_names = (
                name for name, function in toolkit.functions.items() if context.tool_function_filter(function)
            )
            visible_async_names = (
                name for name, function in toolkit.async_functions.items() if context.tool_function_filter(function)
            )
            function_names = dict.fromkeys((*visible_sync_names, *visible_async_names))
        grants.extend(ScriptToolGrant(toolkit_name, function_name) for function_name in function_names)
    return tuple(grants)
