"""API endpoints for Matrix operations."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.logging_config import get_logger
from mindroom.matrix import MATRIX_HOMESERVER
from mindroom.matrix.client import get_joined_rooms, get_room_name, leave_room
from mindroom.matrix.rooms import resolve_room_aliases
from mindroom.matrix.users import create_agent_user, login_agent_user

logger = get_logger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])


class RoomLeaveRequest(BaseModel):
    """Request to leave a room."""

    agent_id: str
    room_id: str


class RoomInfo(BaseModel):
    """Information about a room."""

    room_id: str
    name: str | None = None


class AgentRoomsResponse(BaseModel):
    """Response containing agent rooms information."""

    agent_id: str
    display_name: str
    configured_rooms: list[str]
    joined_rooms: list[str]
    unconfigured_rooms: list[str]
    unconfigured_room_details: list[RoomInfo] = []


class AllAgentsRoomsResponse(BaseModel):
    """Response containing all agents' room information."""

    agents: list[AgentRoomsResponse]


async def get_agent_matrix_rooms(agent_id: str, agent_data: dict[str, Any]) -> AgentRoomsResponse | None:
    """Get Matrix rooms for a specific agent.

    Args:
        agent_id: The agent identifier
        agent_data: The agent configuration data

    Returns:
        AgentRoomsResponse with room information or None if failed

    """
    try:
        # Create or get the agent user
        agent_user = await create_agent_user(
            MATRIX_HOMESERVER,
            agent_id,
            agent_data.get("display_name", agent_id),
        )

        # Login and get the client
        client = await login_agent_user(MATRIX_HOMESERVER, agent_user)

        # Get all joined rooms from Matrix
        joined_rooms = await get_joined_rooms(client)

        if joined_rooms is None:
            await client.close()
            logger.error(f"Failed to get joined rooms for agent {agent_id}")
            return None

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
            unconfigured_room_details.append(RoomInfo(room_id=room_id, name=room_name))

        # Close the client connection
        await client.close()

        return AgentRoomsResponse(
            agent_id=agent_id,
            display_name=agent_data.get("display_name", agent_id),
            configured_rooms=configured_room_ids,  # Return the resolved IDs, not aliases
            joined_rooms=joined_rooms,
            unconfigured_rooms=unconfigured_rooms,
            unconfigured_room_details=unconfigured_room_details,
        )

    except Exception:
        logger.exception(f"Error getting rooms for agent {agent_id}")
        return None


@router.get("/agents/rooms")
async def get_all_agents_rooms() -> AllAgentsRoomsResponse:
    """Get room information for all agents.

    Returns information about configured rooms, joined rooms,
    and unconfigured rooms (joined but not in config) for each agent.
    """
    from .main import config, config_lock

    agents_rooms = []

    with config_lock:
        agents = config.get("agents", {})

    # Gather room information for all agents concurrently
    tasks = []
    for agent_id, agent_data in agents.items():
        tasks.append(get_agent_matrix_rooms(agent_id, agent_data))

    results = await asyncio.gather(*tasks)

    # Filter out None results
    agents_rooms = [result for result in results if result is not None]

    return AllAgentsRoomsResponse(agents=agents_rooms)


@router.get("/agents/{agent_id}/rooms")
async def get_agent_rooms(agent_id: str) -> AgentRoomsResponse:
    """Get room information for a specific agent.

    Args:
        agent_id: The agent identifier

    Returns:
        Room information for the agent

    Raises:
        HTTPException: If agent not found or error occurs

    """
    from .main import config, config_lock

    with config_lock:
        agents = config.get("agents", {})
        if agent_id not in agents:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        agent_data = agents[agent_id]

    result = await get_agent_matrix_rooms(agent_id, agent_data)
    if result is None:
        raise HTTPException(status_code=500, detail=f"Failed to get rooms for agent {agent_id}")

    return result


@router.post("/rooms/leave")
async def leave_room_endpoint(request: RoomLeaveRequest) -> dict[str, bool]:
    """Make an agent leave a specific room.

    Args:
        request: Contains agent_id and room_id

    Returns:
        Success status

    Raises:
        HTTPException: If agent not found or leave operation fails

    """
    from .main import config, config_lock

    with config_lock:
        agents = config.get("agents", {})
        if request.agent_id not in agents:
            raise HTTPException(status_code=404, detail=f"Agent {request.agent_id} not found")

    try:
        # Get agent configuration
        agent_data = agents.get(request.agent_id, {})

        # Create or get the agent user
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

    except Exception as e:
        logger.exception(f"Error leaving room {request.room_id} for agent {request.agent_id}")
        raise HTTPException(status_code=500, detail=f"Error leaving room: {e!s}") from e


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
