"""API endpoints for Matrix operations."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mindroom import constants
from mindroom.logging_config import get_logger
from mindroom.matrix.client import get_joined_rooms, get_room_name, leave_room
from mindroom.matrix.rooms import resolve_room_aliases
from mindroom.matrix.users import create_agent_user, login_agent_user

logger = get_logger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])


class RoomLeaveRequest(BaseModel):
    """Request for an agent or team to leave a room."""

    agent_id: str
    room_id: str


class _RoomInfo(BaseModel):
    """Information about a room."""

    room_id: str
    name: str | None = None


class AgentRoomsResponse(BaseModel):
    """Response containing Matrix entity room information."""

    agent_id: str
    display_name: str
    configured_rooms: list[str]
    joined_rooms: list[str]
    unconfigured_rooms: list[str]
    unconfigured_room_details: list[_RoomInfo] = Field(default_factory=list)


class AllAgentsRoomsResponse(BaseModel):
    """Response containing all configured Matrix entities' room information."""

    agents: list[AgentRoomsResponse]


def _get_configured_matrix_entities(config_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return configured agents and teams keyed by their Matrix entity ID."""
    return {
        **config_data.get("agents", {}),
        **config_data.get("teams", {}),
    }


def _get_configured_matrix_entity(
    config_data: dict[str, Any],
    entity_id: str,
) -> dict[str, Any]:
    """Return one configured Matrix entity or raise a 404."""
    entities = _get_configured_matrix_entities(config_data)
    if entity_id not in entities:
        raise HTTPException(status_code=404, detail=f"Agent or team {entity_id} not found")
    return entities[entity_id]


async def _get_agent_matrix_rooms(
    agent_id: str,
    agent_data: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> AgentRoomsResponse:
    """Get Matrix rooms for a specific configured agent or team.

    Args:
        agent_id: The agent or team identifier
        agent_data: The entity configuration data
        runtime_paths: Runtime context used for homeserver and env-dependent resolution

    Returns:
        AgentRoomsResponse with room information

    """
    # Create or get the agent user
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    agent_user = await create_agent_user(
        homeserver,
        agent_id,
        agent_data.get("display_name", agent_id),
        runtime_paths=runtime_paths,
    )

    # Login and get the client
    client = await login_agent_user(homeserver, agent_user, runtime_paths)

    # Get all joined rooms from Matrix
    joined_rooms = await get_joined_rooms(client) or []

    # Get configured rooms from config (these are aliases like "lobby", "analysis")
    configured_room_aliases = agent_data.get("rooms", [])

    # Resolve room aliases to room IDs for comparison
    configured_room_ids = resolve_room_aliases(configured_room_aliases, runtime_paths=runtime_paths)

    # Calculate unconfigured rooms (joined but not in config)
    unconfigured_rooms = [room for room in joined_rooms if room not in configured_room_ids]

    # Get room names for unconfigured rooms
    unconfigured_room_details = []
    for room_id in unconfigured_rooms:
        room_name = await get_room_name(client, room_id)
        unconfigured_room_details.append(_RoomInfo(room_id=room_id, name=room_name))

    await client.close()

    return AgentRoomsResponse(
        agent_id=agent_id,
        display_name=agent_data.get("display_name", agent_id),
        configured_rooms=configured_room_ids,
        joined_rooms=joined_rooms,
        unconfigured_rooms=unconfigured_rooms,
        unconfigured_room_details=unconfigured_room_details,
    )


@router.get("/agents/rooms")
async def get_all_agents_rooms(request: Request) -> AllAgentsRoomsResponse:
    """Get room information for all configured agents and teams.

    Returns information about configured rooms, joined rooms,
    and unconfigured rooms (joined but not in config) for each Matrix entity.
    """
    from mindroom.api.main import api_config_data, api_config_lock, api_runtime_paths  # noqa: PLC0415

    with api_config_lock(request):
        entities = _get_configured_matrix_entities(api_config_data(request))

    runtime_paths = api_runtime_paths(request)

    # Gather room information for all configured Matrix entities concurrently.
    tasks = [_get_agent_matrix_rooms(agent_id, agent_data, runtime_paths) for agent_id, agent_data in entities.items()]
    agents_rooms = await asyncio.gather(*tasks)

    return AllAgentsRoomsResponse(agents=agents_rooms)


@router.get("/agents/{agent_id}/rooms")
async def get_agent_rooms(agent_id: str, request: Request) -> AgentRoomsResponse:
    """Get room information for a specific configured agent or team.

    Args:
        agent_id: The agent or team identifier
        request: FastAPI request carrying the API runtime context

    Returns:
        Room information for the configured Matrix entity

    Raises:
        HTTPException: If the entity is not found or an error occurs

    """
    from mindroom.api.main import api_config_data, api_config_lock, api_runtime_paths  # noqa: PLC0415

    with api_config_lock(request):
        agent_data = _get_configured_matrix_entity(api_config_data(request), agent_id)

    return await _get_agent_matrix_rooms(agent_id, agent_data, api_runtime_paths(request))


@router.post("/rooms/leave")
async def leave_room_endpoint(request: RoomLeaveRequest, api_request: Request) -> dict[str, bool]:
    """Make an agent or team leave a specific room.

    Args:
        request: Contains the agent/team ID and room ID
        api_request: FastAPI request carrying the API runtime context

    Returns:
        Success status

    Raises:
        HTTPException: If the entity is not found or the leave operation fails

    """
    from mindroom.api.main import api_config_data, api_config_lock, api_runtime_paths  # noqa: PLC0415

    with api_config_lock(api_request):
        agent_data = _get_configured_matrix_entity(api_config_data(api_request), request.agent_id)

    runtime_paths = api_runtime_paths(api_request)
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)

    # Create or get the Matrix user for this configured entity.
    agent_user = await create_agent_user(
        homeserver,
        request.agent_id,
        agent_data.get("display_name", request.agent_id),
        runtime_paths=runtime_paths,
    )

    # Login and get the client
    client = await login_agent_user(homeserver, agent_user, runtime_paths)

    # Leave the room
    success = await leave_room(client, request.room_id)

    # Close the client connection
    await client.close()

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to leave room {request.room_id}")
    return {"success": True}


@router.post("/rooms/leave-bulk")
async def leave_rooms_bulk(requests: list[RoomLeaveRequest], api_request: Request) -> dict[str, Any]:
    """Make multiple agents leave multiple rooms.

    Args:
        requests: List of leave requests
        api_request: FastAPI request carrying the API runtime context

    Returns:
        Results for each request

    """
    results = []
    for request in requests:
        try:
            await leave_room_endpoint(request, api_request)
            results.append({"agent_id": request.agent_id, "room_id": request.room_id, "success": True})
        except HTTPException as e:
            results.append(
                {
                    "agent_id": request.agent_id,
                    "room_id": request.room_id,
                    "success": False,
                    "error": e.detail,
                },
            )

    return {"results": results, "success": all(r["success"] for r in results)}
