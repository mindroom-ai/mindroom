"""Side-effect-free current local grants for saved and live tool work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.agent_policy import is_learning_enabled, resolve_agent_policy_from_data
from mindroom.knowledge.utils import agent_knowledge_authority_signature
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.tool_jobs.agno_compat_functions import function_actor
from mindroom.tool_system.construction import get_toolkit_construction, tool_config_signature
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.filters import tool_name_allowed
from mindroom.tool_system.registry_state import TOOL_METADATA, tool_registry_origins

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def bind_actor_authority[Actor: Agent | Team](actor: Actor, snapshot: dict[str, Any]) -> Actor:
    """Bind current construction evidence outside SDK-persisted session metadata."""
    vars(actor)["mindroom_tool_authority"] = snapshot
    return actor


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
    return {
        "scope": policy.effective_execution_scope,
        "knowledge": agent_knowledge_authority_signature(agent_name, config),
    }


def function_authority(function: Function) -> dict[str, Any]:
    """Read the construction snapshot retained by the actual executing actor."""
    actor = function_actor(function)
    snapshot = vars(actor).get("mindroom_tool_authority", {}) if actor is not None else {}
    toolkit = function.source_toolkit
    construction = get_toolkit_construction(toolkit) if isinstance(toolkit, Toolkit) else None
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


def _framework_tool_allowed(
    config: Config,
    agent_name: str,
    tool_name: str,
    origin: dict[str, Any],
    authority: dict[str, Any],
) -> bool:
    agent = config.agents[agent_name]
    module = str(origin.get("module", ""))
    qualname = str(origin.get("qualname", ""))
    skill_tools = {
        "get_skill_instructions": ("agno.skills.agent_skills", "Skills._get_skill_instructions"),
        "get_skill_reference": ("agno.skills.agent_skills", "Skills._get_skill_reference"),
        "get_skill_script": ("mindroom.tool_system.skills", "_MindroomSkills._get_skill_script"),
    }
    if (module, qualname) == skill_tools.get(tool_name):
        return origin.get("workspace_skill") is True or origin.get("skill_name") in agent.skills
    if (
        module == "agno.agent._default_tools"
        and tool_name == "search_knowledge_base"
        and "create_knowledge_search_tool." in qualname
    ):
        current = agent_knowledge_authority_signature(agent_name, config)
        return current is not None and authority.get("knowledge") == current
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
    return (
        is_learning_enabled(agent, config.defaults) and mode == "agentic" and tool_name in expected.get(module, set())
    )


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
    policy = resolve_agent_policy_from_data(
        owner.agent_name,
        config.agents[owner.agent_name],
        default_worker_scope=config.defaults.worker_scope,
    )
    if "scope" not in authority or authority["scope"] != policy.effective_execution_scope:
        return False
    if toolkit_name is None:
        # Framework-owned knowledge, memory, and skill functions have no authored toolkit.
        return _framework_tool_allowed(config, owner.agent_name, tool_name, origin, authority)
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
    if entry is None or (entry.name == "memory" and view.memory_backend == "none"):
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
    authored_filter: dict[str, Any] = dict(entry.tool_config_overrides)
    if server_id is None:
        return construction.get("config_signature") == tool_config_signature(authored_filter) and tool_name_allowed(
            filtered_name,
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
