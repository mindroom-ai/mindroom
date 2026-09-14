"""Recover tools required by saved approvals without changing session selection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.mcp.config import resolved_mcp_tool_prefix
from mindroom.mcp.function_surface import catalog_function_names_for_tool_config
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def required_approval_tool_names(
    agent_name: str,
    function_names: frozenset[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> tuple[str, ...]:
    """Resolve saved function names through the current agent's permitted tools.

    OAuth catalogs and dynamic selections are process-local, so neither can be
    assumed warm when an approval resumes. Only discover matching MCP servers,
    using the same credential routing as normal agent construction. Return
    run-local additions; a newer session load or unload remains authoritative.
    """
    if not function_names:
        return ()
    await asyncio.to_thread(ensure_tool_registry_loaded, runtime_paths, config)
    surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        loaded_tools=[entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs],
        include_matrix_room_runtime_tools=execution_identity.room_id is not None,
    )
    owners: dict[str, set[str]] = {name: set() for name in function_names}
    for entry in surface.runtime_tool_configs:
        metadata = TOOL_METADATA.get(entry.name)
        if metadata is not None and metadata.requires_room_context and execution_identity.room_id is None:
            continue
        matching_names = function_names.intersection(metadata.function_names if metadata is not None else ())
        server_id = mcp_server_id_from_tool_name(entry.name)
        if server_id is not None:
            server_config = config.mcp_servers.get(server_id)
            if server_config is None or not server_config.enabled:
                continue
            prefix = f"{resolved_mcp_tool_prefix(server_id, server_config)}_"
            if any(name.startswith(prefix) for name in function_names - matching_names):
                manager = require_mcp_server_manager()
                if manager is None:
                    msg = f"MCP tool {entry.name!r} is unavailable for approval continuation"
                    raise RuntimeError(msg)
                runtime = await asyncio.to_thread(
                    resolve_agent_runtime,
                    agent_name,
                    config,
                    runtime_paths,
                    execution_identity=execution_identity,
                    create=False,
                )
                catalog = await manager.get_request_catalog(
                    server_id,
                    credentials_manager=get_runtime_credentials_manager(runtime_paths),
                    worker_target=build_agent_toolkit_worker_target(
                        runtime.execution.execution_scope,
                        agent_name,
                        is_private=runtime.execution.is_private,
                        execution_identity=execution_identity,
                        runtime_paths=runtime_paths,
                    ),
                    expected_config=config,
                )
                matching_names |= function_names.intersection(catalog_function_names_for_tool_config(catalog, entry))
        for name in matching_names:
            owners[name].add(entry.authored_name or entry.name)
    if any(len(tool_names) > 1 for tool_names in owners.values()):
        msg = "Saved approval function has ambiguous configured tool ownership"
        raise RuntimeError(msg)
    return tuple(sorted({owner for tool_names in owners.values() for owner in tool_names}))
