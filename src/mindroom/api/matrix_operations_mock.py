"""Mock API endpoints for Matrix operations - for demonstration purposes."""

import asyncio
import random
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/matrix-mock", tags=["matrix-mock"])


class RoomLeaveRequest(BaseModel):
    """Request to leave a room."""

    agent_id: str
    room_id: str


class AgentRoomsResponse(BaseModel):
    """Response containing agent rooms information."""

    agent_id: str
    display_name: str
    configured_rooms: list[str]
    joined_rooms: list[str]
    unconfigured_rooms: list[str]


class AllAgentsRoomsResponse(BaseModel):
    """Response containing all agents' room information."""

    agents: list[AgentRoomsResponse]


def generate_mock_room_ids(count: int) -> list[str]:
    """Generate mock Matrix room IDs."""
    rooms = []
    for i in range(count):
        room_type = random.choice(["room", "dm", "space"])
        server = random.choice(["localhost", "matrix.org", "example.com"])
        rooms.append(f"!{room_type}{i:03d}:{server}")
    return rooms


async def get_agent_matrix_rooms_mock(agent_id: str, agent_data: dict[str, Any]) -> AgentRoomsResponse:
    """Get mock Matrix rooms for a specific agent.

    This simulates having some unconfigured rooms that agents have joined
    but are not in the configuration (like DM rooms or external invites).
    """
    # Get configured rooms from the agent data
    configured_rooms = agent_data.get("rooms", [])

    # Simulate additional rooms the agent has joined
    # For demo purposes, add 0-3 unconfigured rooms per agent
    num_extra_rooms = random.randint(0, 3)
    unconfigured_rooms = generate_mock_room_ids(num_extra_rooms)

    # The joined rooms are both configured and unconfigured
    joined_rooms = configured_rooms + unconfigured_rooms

    return AgentRoomsResponse(
        agent_id=agent_id,
        display_name=agent_data.get("display_name", agent_id),
        configured_rooms=configured_rooms,
        joined_rooms=joined_rooms,
        unconfigured_rooms=unconfigured_rooms,
    )


@router.get("/agents/rooms", response_model=AllAgentsRoomsResponse)
async def get_all_agents_rooms_mock() -> AllAgentsRoomsResponse:
    """Get mock room information for all agents.

    Returns simulated information about configured rooms, joined rooms,
    and unconfigured rooms for demonstration purposes.
    """
    from .main import config, config_lock

    agents_rooms = []

    with config_lock:
        agents = config.get("agents", {})

    # Generate mock room information for all agents
    for agent_id, agent_data in agents.items():
        agent_rooms = await get_agent_matrix_rooms_mock(agent_id, agent_data)
        agents_rooms.append(agent_rooms)

    return AllAgentsRoomsResponse(agents=agents_rooms)


@router.get("/agents/{agent_id}/rooms", response_model=AgentRoomsResponse)
async def get_agent_rooms_mock(agent_id: str) -> AgentRoomsResponse:
    """Get mock room information for a specific agent."""
    from .main import config, config_lock

    with config_lock:
        agents = config.get("agents", {})
        if agent_id not in agents:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        agent_data = agents[agent_id]

    return await get_agent_matrix_rooms_mock(agent_id, agent_data)


@router.post("/rooms/leave")
async def leave_room_mock(request: RoomLeaveRequest) -> dict[str, bool]:
    """Mock endpoint to simulate leaving a room."""
    from .main import config, config_lock

    with config_lock:
        agents = config.get("agents", {})
        if request.agent_id not in agents:
            raise HTTPException(status_code=404, detail=f"Agent {request.agent_id} not found")

    # Simulate a small delay for the operation
    await asyncio.sleep(0.1)

    logger.info(f"Mock: Agent {request.agent_id} left room {request.room_id}")
    return {"success": True}


@router.post("/rooms/leave-bulk")
async def leave_rooms_bulk_mock(requests: list[RoomLeaveRequest]) -> dict[str, Any]:
    """Mock endpoint to simulate bulk room leaving."""
    results = []
    for request in requests:
        try:
            result = await leave_room_mock(request)
            results.append(
                {
                    "agent_id": request.agent_id,
                    "room_id": request.room_id,
                    "success": True,
                },
            )
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
