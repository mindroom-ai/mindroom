"""Side-effect-free current local grants for saved and live tool work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.agent_policy import resolve_agent_policy_from_data
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.tool_system.construction import get_toolkit_construction
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.registry_state import TOOL_METADATA, tool_registry_origins

if TYPE_CHECKING:
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

AUTHORITY_METADATA_KEY = "mindroom_tool_authority"


def bind_toolkit_authority(toolkit: Toolkit, *, authored_name: str) -> None:
    """Attach the authored approval owner while retaining the concrete source toolkit."""
    for function in toolkit.get_async_functions().values():
        function.owning_toolkit = authored_name
        function.source_toolkit = toolkit


def authority_snapshot(config: Config, agent_name: str) -> dict[str, Any]:
    """Freeze the actor execution scope without constructing tools."""
    policy = resolve_agent_policy_from_data(
        agent_name,
        config.agents[agent_name],
        default_worker_scope=config.defaults.worker_scope,
    )
    return {"scope": policy.effective_execution_scope}


def function_authority(function: Function) -> dict[str, Any]:
    """Read the construction snapshot retained by the actual executing actor."""
    actor = function._agent or function._team
    snapshot = (actor.metadata or {}).get(AUTHORITY_METADATA_KEY, {}) if actor is not None else {}
    toolkit = function.source_toolkit
    construction = get_toolkit_construction(toolkit) if isinstance(toolkit, Toolkit) else None
    return {
        "scope": snapshot.get("scope"),
        "construction": (
            {
                "name": construction.name,
                "factory_origin": list(construction.factory_origin)
                if construction.factory_origin is not None
                else None,
            }
            if construction is not None
            else None
        ),
    }


def _framework_tool_allowed(config: Config, agent_name: str, tool_name: str, origin: dict[str, Any]) -> bool:
    agent = config.agents[agent_name]
    module = str(origin.get("module", ""))
    qualname = str(origin.get("qualname", ""))
    if (
        module == "agno.agent._default_tools"
        and tool_name == "search_knowledge_base"
        and "create_knowledge_search_tool." in qualname
    ):
        return bool(
            agent.knowledge_bases
            or (agent.private is not None and agent.private.knowledge is not None)
            or config.resolve_entity(agent_name).memory_backend == "file",
        )
    learning = agent.learning if agent.learning is not None else config.defaults.learning
    mode = agent.learning_mode or config.defaults.learning_mode
    expected = {
        "agno.learn.stores.user_profile": {"update_profile"},
        "agno.learn.stores.user_memory": {
            "update_user_memory",
            "add_memory",
            "update_memory",
            "delete_memory",
            "clear_all_memories",
        },
    }
    return learning and mode == "agentic" and tool_name in expected.get(module, set())


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
    if authority:
        policy = resolve_agent_policy_from_data(
            owner.agent_name,
            config.agents[owner.agent_name],
            default_worker_scope=config.defaults.worker_scope,
        )
        if authority.get("scope") != policy.effective_execution_scope:
            return False
    if toolkit_name is None:
        # Framework-owned knowledge and memory functions have no authored toolkit.
        return _framework_tool_allowed(config, owner.agent_name, tool_name, origin)
    construction = authority.get("construction")
    if not isinstance(construction, dict):
        return False
    view = config.resolve_entity(owner.agent_name)
    surface = visible_tool_surface(
        agent_name=owner.agent_name,
        config=config,
        session_id=owner.session_id,
        loaded_tools=[entry.name for entry in view.authored_deferred_tool_configs],
        delegation_depth=depth,
        include_matrix_room_runtime_tools=owner.room_id is not None,
    )
    entry = next(
        (
            item
            for item in surface.runtime_tool_configs
            if (item.authored_name or item.name) == toolkit_name and item.name == construction.get("name")
        ),
        None,
    )
    if entry is None:
        return False
    return _configured_tool_allowed(config, owner, entry, tool_name, origin, construction)


def _configured_tool_allowed(
    config: Config,
    owner: ToolExecutionIdentity,
    entry: EffectiveToolConfig,
    tool_name: str,
    origin: dict[str, Any],
    construction: dict[str, Any],
) -> bool:
    if tool_registry_origins().get(entry.name) != construction.get("factory_origin"):
        return False
    metadata = TOOL_METADATA.get(entry.name)
    if metadata is not None and metadata.requires_room_context and owner.room_id is None:
        return False
    server_id = mcp_server_id_from_tool_name(entry.name)
    filtered_name = tool_name
    filters: list[dict[str, Any]] = [dict(entry.tool_config_overrides)]
    if server_id is not None:
        server = config.mcp_servers.get(server_id)
        if server is None or not server.enabled or origin.get("mcp_server_id") != server_id:
            return False
        filtered_name = origin.get("mcp_tool_name")
        if filtered_name is None:
            return True  # OAuth status/list operations do not invoke a remote tool.
        filters.append({"include_tools": server.include_tools, "exclude_tools": server.exclude_tools})
    return all(
        (not item.get("include_tools") or filtered_name in item["include_tools"])
        and filtered_name not in item.get("exclude_tools", [])
        for item in filters
    )
