"""Capability and approval policy for background script tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.script_runs.models import ScriptToolGrant
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context

if TYPE_CHECKING:
    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = ["resolve_current_script_tool", "resolve_script_launch_grants"]


_SCRIPT_RESTRICTED_TOOLKITS = frozenset(
    {"script", "compact_context", "delegate", "dynamic_tools", "dynamic_workflow", "memory", "self_config"},
)


def resolve_script_launch_grants(
    context: ToolRuntimeContext,
) -> tuple[ScriptToolGrant, ...]:
    """Resolve the stable registered function surface captured when a script launches."""
    grants, _ = _resolve_script_tool_surface(context, context.config)
    return grants


def resolve_current_script_tool(
    context: ToolRuntimeContext,
    grant: ScriptToolGrant,
) -> Toolkit | None:
    """Build the requested live toolkit only when its granted function remains visible."""
    config = context.current_config
    if context.agent_name not in config.agents:
        return None

    tool_entry = _current_tool_entry(context, config, grant)
    if tool_entry is None:
        return None

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
    toolkit = build_agent_toolkit(
        grant.toolkit_name,
        agent_name=context.agent_name,
        config=config,
        runtime_paths=context.runtime_paths,
        worker_tools=resolve_runtime_worker_tools(
            context.agent_name,
            config,
            context.runtime_paths,
            [grant.toolkit_name],
            tool_registry_preloaded=True,
        ),
        runtime_overrides=entity_view.tool_runtime_overrides(grant.toolkit_name),
        agent_runtime=agent_runtime,
        tool_config_overrides=tool_entry.tool_config_overrides,
        execution_identity=execution_identity,
        session_id=context.session_id,
    )
    if toolkit is None:
        return None

    function = toolkit.functions.get(grant.function_name) or toolkit.async_functions.get(grant.function_name)
    if function is None or (
        context.tool_function_filter is not None and not context.tool_function_filter(function)
    ):
        toolkit.close()
        return None
    return toolkit


def _current_tool_entry(
    context: ToolRuntimeContext,
    config: Config,
    grant: ScriptToolGrant,
) -> EffectiveToolConfig | None:
    ensure_tool_registry_loaded(context.runtime_paths, config)
    entity_view = config.resolve_entity(context.agent_name)
    all_deferred_tools = [entry.name for entry in entity_view.authored_deferred_tool_configs]
    return next(
        (
            entry
            for entry in visible_tool_surface(
                agent_name=context.agent_name,
                config=config,
                loaded_tools=all_deferred_tools,
                enable_dynamic_tools_manager=False,
            ).runtime_tool_configs
            if entry.name == grant.toolkit_name
            and entry.name in TOOL_METADATA
            and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
        ),
        None,
    )


def _resolve_script_tool_surface(
    context: ToolRuntimeContext,
    config: Config,
) -> tuple[tuple[ScriptToolGrant, ...], dict[str, Toolkit]]:
    if context.agent_name not in config.agents:
        msg = f"Background scripts require a configured agent owner; {context.agent_name!r} is not an agent."
        raise ValueError(msg)

    ensure_tool_registry_loaded(context.runtime_paths, config)
    entity_view = config.resolve_entity(context.agent_name)
    all_deferred_tools = [entry.name for entry in entity_view.authored_deferred_tool_configs]
    tool_configs = tuple(
        entry
        for entry in visible_tool_surface(
            agent_name=context.agent_name,
            config=config,
            loaded_tools=all_deferred_tools,
            enable_dynamic_tools_manager=False,
        ).runtime_tool_configs
        if entry.name in TOOL_METADATA and entry.name not in _SCRIPT_RESTRICTED_TOOLKITS
    )
    tool_names = [entry.name for entry in tool_configs]

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
    toolkits: dict[str, Toolkit] = {}
    for tool_entry in tool_configs:
        toolkit_name = tool_entry.name
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
            toolkits[toolkit_name] = toolkit

    grants: list[ScriptToolGrant] = []
    for toolkit_name, toolkit in toolkits.items():
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
    return tuple(grants), toolkits
