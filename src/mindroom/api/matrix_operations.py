"""API endpoints for Matrix operations."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mindroom.constants import MATRIX_HOMESERVER
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


async def _get_agent_matrix_rooms(agent_id: str, agent_data: dict[str, Any]) -> AgentRoomsResponse:
    """Get Matrix rooms for a specific configured agent or team.

    Args:
        agent_id: The agent or team identifier
        agent_data: The entity configuration data

    Returns:
        AgentRoomsResponse with room information

    """
    # Create or get the agent user
    agent_user = await create_agent_user(
        MATRIX_HOMESERVER,
        agent_id,
        agent_data.get("display_name", agent_id),
    )

    # Login and get the client
    client = await login_agent_user(MATRIX_HOMESERVER, agent_user)

    # Get all joined rooms from Matrix
    joined_rooms = await get_joined_rooms(client) or []

    # Get configured rooms from config (these are aliases like "lobby", "analysis")
    configured_room_aliases = agent_data.get("rooms", [])

    # Resolve room aliases to room IDs for comparison
    configured_room_ids = resolve_room_aliases(configured_room_aliases)

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
async def get_all_agents_rooms() -> AllAgentsRoomsResponse:
    """Get room information for all configured agents and teams.

    Returns information about configured rooms, joined rooms,
    and unconfigured rooms (joined but not in config) for each Matrix entity.
    """
    from mindroom.api.main import config, config_lock  # noqa: PLC0415

    with config_lock:
        entities = _get_configured_matrix_entities(config)

    # Gather room information for all configured Matrix entities concurrently.
    tasks = [_get_agent_matrix_rooms(agent_id, agent_data) for agent_id, agent_data in entities.items()]
    agents_rooms = await asyncio.gather(*tasks)

    return AllAgentsRoomsResponse(agents=agents_rooms)


@router.get("/agents/{agent_id}/rooms")
async def get_agent_rooms(agent_id: str) -> AgentRoomsResponse:
    """Get room information for a specific configured agent or team.

    Args:
        agent_id: The agent or team identifier

    Returns:
        Room information for the configured Matrix entity

    Raises:
        HTTPException: If the entity is not found or an error occurs

    """
    from mindroom.api.main import config, config_lock  # noqa: PLC0415

    with config_lock:
        agent_data = _get_configured_matrix_entity(config, agent_id)

    return await _get_agent_matrix_rooms(agent_id, agent_data)


@router.post("/rooms/leave")
async def leave_room_endpoint(request: RoomLeaveRequest) -> dict[str, bool]:
    """Make an agent or team leave a specific room.

    Args:
        request: Contains the agent/team ID and room ID

    Returns:
        Success status

    Raises:
        HTTPException: If the entity is not found or the leave operation fails

    """
    from mindroom.api.main import config, config_lock  # noqa: PLC0415

    with config_lock:
        agent_data = _get_configured_matrix_entity(config, request.agent_id)

    # Create or get the Matrix user for this configured entity.
    agent_user = await create_agent_user(
        MATRIX_HOMESERVER,
        request.agent_id,
        agent_data.get("display_name", request.agent_id),
    )

    # Login and get the client
    client = await login_agent_user(MATRIX_HOMESERVER, agent_user)

    # Leave the room
    success = await leave_room(client, request.room_id)

    # Close the client connection
    await client.close()

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to leave room {request.room_id}")
    return {"success": True}


@router.post("/rooms/leave-bulk")
async def leave_rooms_bulk(requests: list[RoomLeaveRequest]) -> dict[str, Any]:
    """Make multiple agents leave multiple rooms.

    Args:
        requests: List of leave requests

    Returns:
        Results for each request

    """
    results = []
    for request in requests:
        try:
            await leave_room_endpoint(request)
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
