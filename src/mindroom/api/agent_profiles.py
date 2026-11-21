"""Chat clients' read of what one of a room's agents is: its role, the model it runs there, and its toolkits.

The profile opens only while the room and the agent's access rules let the caller use that agent there, the check the
workspace and terminal routes share. Its toolkits are a catalog of what the agent's config gives its Matrix turns in
the room, not what a turn for this caller would build: it builds no toolkit and asks no MCP server, so approval rules,
stored settings and a toolkit's own construction may still hide functions. It leaves out a toolkit whose authored
settings switch off every function. Every caller it admits sees each toolkit's registry name, display name, description
and category, and an MCP server's configured ID, text an operator may write for display; never a toolkit's settings,
credentials, environment, or an MCP server's address or headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from mindroom.ai_run_metadata import model_payload
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.plugin_http import RoomReader, require_room_reader
from mindroom.room_agent_access import offered_functions
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.turn_model import resolve_inherited_model

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["agent-profiles"])

_Room = Annotated[RoomReader, Depends(require_room_reader)]


@dataclass(frozen=True)
class _ProfileTool:
    """One toolkit the agent's turns in the room may build, as the tool registry describes it."""

    name: str
    display_name: str
    description: str
    category: str
    mcp_server: str | None
    """The configured MCP server's ID when the toolkit is one, so clients can show it as a connected app."""


@dataclass(frozen=True)
class AgentProfile:
    """What the agent is for, the model its turns in the room run without a thread choice, and its toolkits."""

    role: str | None
    model: dict[str, str]
    """The shape of ``io.mindroom.ai_run``'s ``model``: ``config``, and ``display_name``, ``id``, ``provider`` if known."""
    tools: list[_ProfileTool]
    """In the order a Matrix room turn of the agent builds them, deferred toolkits included."""


@router.get("/agents/{agent_user_id}/profile")
async def read_profile(request: Request, room: _Room, room_id: str, agent_user_id: str) -> AgentProfile:
    """Return the profile of an agent the caller may use in the room; 403 for any other user ID, as ``agent()`` does.

    Its toolkits are those the agent's config gives its Matrix turns in the room, which a turn for the caller may narrow.
    """
    workspace = await room.agent(agent_user_id)
    config = workspace.config
    runtime_paths = bind_current_request_snapshot(request).runtime_paths
    model_name, _source = resolve_inherited_model(config, runtime_paths, workspace.name, room_id)
    registry = config_plugins(config, runtime_paths).tools
    tools: list[_ProfileTool] = []
    for entry in resolve_agent_tools(config, workspace.name, registry).candidates():
        metadata = registry.get(entry.name)
        if metadata is None:  # A preset or a toolkit MindRoom adds without a registry entry.
            continue
        functions, _writes = offered_functions(metadata, entry.tool_config_overrides)
        if functions is not None and not functions:
            continue
        tools.append(
            _ProfileTool(
                metadata.name,
                metadata.display_name,
                metadata.description,
                metadata.category.value,
                metadata.mcp_server_id,
            ),
        )
    return AgentProfile(
        role=config.agents[workspace.name].role.strip() or None,
        model=model_payload(config, model_name),
        tools=tools,
    )
