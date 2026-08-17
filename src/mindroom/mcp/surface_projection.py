"""Project configured tools and live MCP catalogs onto provider-visible surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from types import FunctionType, MethodType
from typing import TYPE_CHECKING

from agno.tools.function import Function

from mindroom.logging_config import get_logger
from mindroom.mcp.config import mcp_oauth_bridge_function_names
from mindroom.mcp.function_surface import (
    MCPFunctionCollisionReport,
    MCPFunctionSurfaceSnapshot,
    analyze_mcp_function_collisions,
)
from mindroom.mcp.registry import mcp_server_id_from_tool_name, mcp_tool_name
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded, get_tool_by_name
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.constants import RuntimePaths
    from mindroom.mcp.types import MCPServerCatalog, MCPServerState

type _LoadedToolNames = list[str] | tuple[str, ...] | set[str] | frozenset[str]

__all__ = [
    "MCPFunctionSurfaceContext",
    "MCPScopedFunctionState",
    "function_collision_messages",
    "function_collision_reports",
    "mcp_tool_unavailable_messages",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MCPScopedFunctionState:
    """One credential surface and the MCP state that can publish onto it."""

    credential_surface: tuple[str, str]
    state: MCPServerState


@dataclass(frozen=True, slots=True)
class MCPFunctionSurfaceContext:
    """Inputs needed to project configured and discovered function surfaces."""

    runtime_paths: RuntimePaths
    config: Config
    states: Mapping[str, MCPServerState]
    scoped_states: tuple[MCPScopedFunctionState, ...]


def _normalized_tool_filter(value: object) -> set[str]:
    """Normalize an MCP assignment's remote tool filter."""
    if value is None:
        return set()
    if isinstance(value, str):
        return {part.strip() for part in value.replace("\n", ",").split(",") if part.strip()}
    if isinstance(value, list):
        return {part.strip() for part in value if isinstance(part, str) and part.strip()}
    return set()


def _catalog_function_names_for_tool_config(
    catalog: MCPServerCatalog,
    tool_config: EffectiveToolConfig,
) -> set[str]:
    """Return catalog function names after one agent MCP assignment's filters."""
    include_tools = _normalized_tool_filter(tool_config.tool_config_overrides.get("include_tools"))
    exclude_tools = _normalized_tool_filter(tool_config.tool_config_overrides.get("exclude_tools"))
    return {
        tool.function_name
        for tool in catalog.tools
        if (not exclude_tools or tool.remote_name not in exclude_tools)
        and (not include_tools or tool.remote_name in include_tools)
    }


def _scoped_state_is_visible_to_agent(scoped: MCPScopedFunctionState, agent_name: str) -> bool:
    """Return whether one credential-scoped catalog belongs on an agent surface."""
    worker_scope, _worker_key = scoped.credential_surface
    return worker_scope not in {"shared", "user_agent"} or scoped.state.oauth_routing_agent_name == agent_name


def _configured_tool_configs(
    context: MCPFunctionSurfaceContext,
    agent_name: str,
    *,
    loaded_tools: _LoadedToolNames | None,
) -> tuple[EffectiveToolConfig, ...]:
    """Return provider-visible tool configs for one agent surface."""
    return visible_tool_surface(
        agent_name=agent_name,
        config=context.config,
        loaded_tools=loaded_tools,
        enable_dynamic_tools_manager=True,
        include_matrix_room_runtime_tools=True,
    ).runtime_tool_configs


def _mcp_server_id_from_tool_config_name(
    context: MCPFunctionSurfaceContext,
    tool_name: str,
) -> str | None:
    """Resolve a configured tool name to an active MCP server."""
    for server_id, server_config in context.config.mcp_servers.items():
        if server_config.enabled and tool_name == mcp_tool_name(server_id):
            return server_id
    registered_server_id = mcp_server_id_from_tool_name(tool_name)
    return registered_server_id if registered_server_id in context.states else None


def _partition_tool_configs(
    context: MCPFunctionSurfaceContext,
    tool_configs: tuple[EffectiveToolConfig, ...],
) -> tuple[list[EffectiveToolConfig], dict[str, tuple[EffectiveToolConfig, ...]]]:
    """Split tool configs into local configs and visible MCP server assignments."""
    local_tool_configs: list[EffectiveToolConfig] = []
    mcp_tool_configs: dict[str, list[EffectiveToolConfig]] = {}
    for tool_config in tool_configs:
        server_id = _mcp_server_id_from_tool_config_name(context, tool_config.name)
        if server_id is not None:
            mcp_tool_configs.setdefault(server_id, []).append(tool_config)
        else:
            local_tool_configs.append(tool_config)
    return local_tool_configs, {server_id: tuple(configs) for server_id, configs in mcp_tool_configs.items()}


def _metadata_only_tool_function_names(
    tool_name: str,
    *,
    config: Config,
    agent_name: str,
) -> set[str]:
    """Return provider-visible names for context-built tools declared in metadata."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None or metadata.factory is not None:
        return set()
    if tool_name == "memory" and config.resolve_entity(agent_name).memory_backend == "none":
        return set()
    return set(metadata.function_names)


def _toolkit_function_names(toolkit: Toolkit) -> set[str]:
    """Return provider-visible function names exposed by one toolkit instance."""
    names = {name for name in {*toolkit.functions, *toolkit.async_functions} if name}
    if names:
        return names
    for raw_tool in toolkit.tools:
        if isinstance(raw_tool, Function) and raw_tool.name:
            names.add(raw_tool.name)
        elif isinstance(raw_tool, FunctionType | MethodType) and raw_tool.__name__:
            names.add(raw_tool.__name__)
    return names


def _configured_function_surface(
    context: MCPFunctionSurfaceContext,
    agent_name: str,
    *,
    loaded_tools: _LoadedToolNames | None,
) -> tuple[set[str], dict[str, tuple[EffectiveToolConfig, ...]]]:
    """Return one agent's provider-visible local functions and MCP assignments."""
    ensure_tool_registry_loaded(context.runtime_paths, context.config)
    local_tool_configs, mcp_tool_configs = _partition_tool_configs(
        context,
        _configured_tool_configs(context, agent_name, loaded_tools=loaded_tools),
    )
    metadata_function_names = {
        tool_config.name: _metadata_only_tool_function_names(
            tool_config.name,
            config=context.config,
            agent_name=agent_name,
        )
        for tool_config in local_tool_configs
    }
    function_names = {function_name for names in metadata_function_names.values() for function_name in names}
    for tool_config in sorted(local_tool_configs, key=lambda entry: entry.name):
        if metadata_function_names[tool_config.name]:
            continue
        try:
            toolkit = get_tool_by_name(
                tool_config.name,
                context.runtime_paths,
                worker_target=None,
                authorization=context.config.authorization,
                tool_config_overrides=dict(tool_config.tool_config_overrides),
            )
        except Exception as exc:
            logger.debug(
                "Skipping local tool during MCP function-name validation",
                tool_name=tool_config.name,
                error=str(exc),
            )
            continue
        function_names.update(_toolkit_function_names(toolkit))
    return function_names, mcp_tool_configs


def _agent_function_surface_snapshot(
    context: MCPFunctionSurfaceContext,
    agent_name: str,
    *,
    loaded_tools: _LoadedToolNames | None,
    credential_surface: tuple[str, str] | None,
    candidate_state: MCPServerState | None = None,
    candidate_catalog: MCPServerCatalog | None = None,
    configured_surface: tuple[set[str], dict[str, tuple[EffectiveToolConfig, ...]]] | None = None,
) -> MCPFunctionSurfaceSnapshot:
    """Snapshot one configured agent and credential surface from supplied runtime state."""
    local_function_names, configured_mcp_tool_configs = configured_surface or _configured_function_surface(
        context,
        agent_name,
        loaded_tools=loaded_tools,
    )
    server_function_sources: list[tuple[str, tuple[frozenset[str], ...]]] = []
    for server_id in sorted(configured_mcp_tool_configs):
        state = context.states.get(server_id)
        if state is None or (state.last_error is not None and state is not candidate_state):
            continue
        function_sources: list[frozenset[str]] = []
        if state.config.auth is not None:
            function_sources.append(frozenset(mcp_oauth_bridge_function_names(server_id, state.config)))
        catalogs = [candidate_catalog if state is candidate_state else state.catalog]
        catalogs.extend(
            candidate_catalog if scoped.state is candidate_state else scoped.state.catalog
            for scoped in context.scoped_states
            if scoped.state.server_id == server_id
            and credential_surface is not None
            and scoped.credential_surface == credential_surface
            and _scoped_state_is_visible_to_agent(scoped, agent_name)
            and (scoped.state.last_error is None or scoped.state is candidate_state)
        )
        function_sources.extend(
            frozenset(
                function_name
                for tool_config in configured_mcp_tool_configs[server_id]
                for function_name in _catalog_function_names_for_tool_config(catalog, tool_config)
            )
            for catalog in catalogs
            if catalog is not None
        )
        server_function_sources.append((server_id, tuple(function_sources)))
    return MCPFunctionSurfaceSnapshot(
        agent_name=agent_name,
        credential_surface=credential_surface,
        local_function_names=frozenset(local_function_names),
        server_function_sources=tuple(server_function_sources),
    )


def function_collision_reports(
    context: MCPFunctionSurfaceContext,
    *,
    candidate_state: MCPServerState | None = None,
    candidate_catalog: MCPServerCatalog | None = None,
) -> tuple[MCPFunctionCollisionReport, ...]:
    """Project all active surfaces and return their collision reports."""
    credential_surface = candidate_state.oauth_credential_scope if candidate_state is not None else None
    credential_surfaces = {
        scoped.credential_surface
        for scoped in context.scoped_states
        if scoped.state.catalog is not None and scoped.state.last_error is None
    } | ({credential_surface} if credential_surface is not None else set())
    configured_surfaces = {
        agent_name: _configured_function_surface(context, agent_name, loaded_tools=[])
        for agent_name in sorted(context.config.agents)
    }
    snapshots = tuple(
        _agent_function_surface_snapshot(
            context,
            agent_name,
            loaded_tools=[],
            credential_surface=surface,
            candidate_state=candidate_state,
            candidate_catalog=candidate_catalog,
            configured_surface=configured_surfaces[agent_name],
        )
        for surface in (None, *sorted(credential_surfaces))
        for agent_name in sorted(context.config.agents)
    )
    return analyze_mcp_function_collisions(snapshots)


def mcp_tool_unavailable_messages(
    context: MCPFunctionSurfaceContext,
    agent_name: str,
    loaded_tools: _LoadedToolNames,
) -> list[str]:
    """Return unavailable non-OAuth server messages for one dynamic-tool surface."""
    _local_tool_configs, mcp_tool_configs = _partition_tool_configs(
        context,
        _configured_tool_configs(context, agent_name, loaded_tools=loaded_tools),
    )
    messages: list[str] = []
    for server_id in sorted(mcp_tool_configs):
        server_config = context.config.mcp_servers.get(server_id)
        state = context.states.get(server_id)
        if server_config is not None and server_config.auth is not None:
            continue
        if state is not None and state.config.auth is not None:
            continue
        if state is None:
            messages.append(f"MCP server '{server_id}' is not configured or has not been synchronized.")
        elif state.last_error is not None:
            messages.append(f"MCP server '{server_id}' is unavailable: {state.last_error}")
        elif state.catalog is None or state.session is None or not state.connected:
            messages.append(f"MCP server '{server_id}' is not connected.")
    return messages


def function_collision_messages(
    context: MCPFunctionSurfaceContext,
    agent_name: str,
    loaded_tools: _LoadedToolNames,
    *,
    credential_surfaces: set[tuple[str, str]],
) -> list[str]:
    """Return collision messages for one candidate dynamic-tool surface."""
    active_states = (*context.states.values(), *(scoped.state for scoped in context.scoped_states))
    if not any(
        state.last_error is None
        and (state.catalog is not None or (state.oauth_credential_scope is None and state.config.auth is not None))
        for state in active_states
    ):
        return []
    configured_surface = _configured_function_surface(context, agent_name, loaded_tools=loaded_tools)
    snapshots = tuple(
        _agent_function_surface_snapshot(
            context,
            agent_name,
            loaded_tools=loaded_tools,
            credential_surface=credential_surface,
            configured_surface=configured_surface,
        )
        for credential_surface in (sorted(credential_surfaces) or [None])
    )
    return sorted(
        {
            message
            for report in analyze_mcp_function_collisions(snapshots)
            for _function_name, message in report.function_name_collisions
        },
    )
