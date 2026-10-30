"""Side-effect-free current local grants for saved and live tool work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.config.knowledge_graph import KNOWLEDGE_GRAPH_EDIT_FUNCTIONS
from mindroom.config.resolution import edits_knowledge_graph
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.tool_system.agent_tools import resolve_agent_tools, turn_room_flags
from mindroom.tool_system.construction import get_toolkit_construction, tool_config_signature
from mindroom.tool_system.filters import tool_name_allowed
from mindroom.tool_system.registry_state import tool_registry_origin

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def authority_snapshot(config: Config, agent_name: str) -> dict[str, Any]:
    """Freeze the actor's execution scope and file access without constructing tools."""
    agent = config.resolve_agent(agent_name)
    return {"scope": agent.policy.effective_execution_scope, "file_access": agent.file_access}


def toolkit_authority(snapshot: Mapping[str, Any], toolkit: AnyToolkit) -> dict[str, Any]:
    """Return the evidence a toolkit's calls are granted by: the actor's snapshot and the toolkit's construction."""
    construction = get_toolkit_construction(toolkit)
    return {
        **snapshot,
        "construction": (
            {
                "name": construction.name,
                "config_signature": construction.config_signature,
                "factory_origin": list(construction.factory_origin)
                if construction.factory_origin is not None
                else None,
            }
            if construction is not None
            else None
        ),
    }


def locally_allowed(
    config: Config,
    owner: ToolExecutionIdentity,
    *,
    tool_name: str,
    toolkit_name: str | None,
    origin: dict[str, Any],
    depth: int,
    authority: dict[str, Any],
) -> bool:
    """Check current authored ownership and filters without remote availability probes."""
    if owner.agent_name not in config.agents:
        return False
    # The execution scope and file access recorded when the toolkit was built must still be the actor's current ones.
    current = authority_snapshot(config, owner.agent_name)
    if any(key not in authority or authority[key] != value for key, value in current.items()):
        return False
    construction = authority.get("construction")
    if not isinstance(construction, dict):
        return False
    room_tools, matrix_room_tools = turn_room_flags(owner)
    candidates = resolve_agent_tools(config, owner.agent_name).candidates(
        depth=depth,
        room_tools=room_tools,
        matrix_room_tools=matrix_room_tools,
        portable_search=True,
    )
    entry = next(
        (
            item
            for item in candidates
            if (item.authored_name or item.name) == toolkit_name and item.name == construction.get("name")
        ),
        None,
    )
    if entry is None:
        return False
    # Graph edits are functions of the knowledge_graph toolkit that only an agent allowed to edit its graph gets.
    edits_graph = entry.name == "knowledge_graph" and tool_name in KNOWLEDGE_GRAPH_EDIT_FUNCTIONS
    if edits_graph and not edits_knowledge_graph(config, owner.agent_name):
        return False
    return _configured_tool_allowed(config, entry, tool_name, origin, construction)


def _configured_tool_allowed(
    config: Config,
    entry: EffectiveToolConfig,
    tool_name: str,
    origin: dict[str, Any],
    construction: dict[str, Any],
) -> bool:
    if tool_registry_origin(entry.name) != construction.get("factory_origin"):
        return False
    server_id = mcp_server_id_from_tool_name(entry.name)
    authored_filter: dict[str, Any] = dict(entry.tool_config_overrides)
    if server_id is None:
        return construction.get("config_signature") == tool_config_signature(authored_filter) and tool_name_allowed(
            tool_name,
            include=authored_filter.get("include_tools"),
            exclude=authored_filter.get("exclude_tools"),
        )
    filters: list[dict[str, Any]] = [authored_filter]
    server = config.mcp_servers.get(server_id)
    if server is None or not server.enabled or origin.get("mcp_server_id") != server_id:
        return False
    filtered_name = origin.get("mcp_tool_name")
    if filtered_name is None:
        return True  # OAuth status/list operations do not invoke a remote tool.
    filters.append({"include_tools": server.include_tools, "exclude_tools": server.exclude_tools})
    return all(
        tool_name_allowed(
            filtered_name,
            include=item.get("include_tools") or None,
            exclude=item.get("exclude_tools"),
        )
        for item in filters
    )
