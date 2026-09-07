"""API endpoints for Matrix operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mindroom.api.config_lifecycle import app_state, read_committed_config_and_runtime, read_committed_runtime_config
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_name
from mindroom.matrix.rooms import filter_non_dm_rooms
from mindroom.matrix.state import resolve_room_aliases
from mindroom.matrix.users import create_agent_http_client

logger = get_logger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])


if TYPE_CHECKING:
    from mindroom import constants
    from mindroom.config.agent import AgentConfig, TeamConfig
    from mindroom.config.main import Config


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


def _get_runtime_matrix_entities(config: Config) -> dict[str, AgentConfig | TeamConfig]:
    """Return runtime-validated agents and teams keyed by their Matrix entity ID."""
    return {
        **config.agents,
        **config.teams,
    }


def _get_runtime_matrix_entity(config: Config, entity_id: str) -> AgentConfig | TeamConfig:
    """Return one runtime-validated Matrix entity or raise a 404."""
    entities = _get_runtime_matrix_entities(config)
    if entity_id not in entities:
        raise HTTPException(status_code=404, detail=f"Agent or team {entity_id} not found")
    return entities[entity_id]


async def _get_agent_matrix_rooms(
    agent_id: str,
    display_name: str,
    configured_room_aliases: list[str],
    runtime_paths: constants.RuntimePaths,
) -> AgentRoomsResponse:
    """Get Matrix rooms for a specific configured agent or team.

    Args:
        agent_id: The agent or team identifier
        display_name: The Matrix display name for the entity
        configured_room_aliases: Room references the entity should treat as configured
        runtime_paths: Runtime context used for homeserver and env-dependent resolution

    Returns:
        AgentRoomsResponse with room information

    """
    client = create_agent_http_client(agent_id, runtime_paths)
    try:
        joined_rooms = await get_joined_rooms(client) or []
        configured_room_ids = resolve_room_aliases(configured_room_aliases, runtime_paths=runtime_paths)
        rooms_not_configured = [room for room in joined_rooms if room not in configured_room_ids]
        unconfigured_rooms = await filter_non_dm_rooms(client, rooms_not_configured)
        unconfigured_room_details = []
        for room_id in unconfigured_rooms:
            room_name = await get_room_name(client, room_id)
            unconfigured_room_details.append(_RoomInfo(room_id=room_id, name=room_name))
    finally:
        await client.close()

    return AgentRoomsResponse(
        agent_id=agent_id,
        display_name=display_name,
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
    config, runtime_paths = read_committed_runtime_config(request)
    entities = _get_runtime_matrix_entities(config)

    # Gather room information for all configured Matrix entities concurrently.
    tasks = [
        _get_agent_matrix_rooms(
            agent_id,
            entity.display_name,
            get_rooms_for_entity(agent_id, config),
            runtime_paths,
        )
        for agent_id, entity in entities.items()
    ]
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
    config, runtime_paths = read_committed_runtime_config(request)
    entity = _get_runtime_matrix_entity(config, agent_id)
    return await _get_agent_matrix_rooms(
        agent_id,
        entity.display_name,
        get_rooms_for_entity(agent_id, config),
        runtime_paths,
    )


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
    read_committed_config_and_runtime(
        api_request,
        lambda config_data: _get_configured_matrix_entity(config_data, request.agent_id),
    )
    leave = app_state(api_request.app).leave_matrix_room
    if leave is None:
        raise HTTPException(status_code=503, detail="Leaving a room requires a running MindRoom instance")
    try:
        success = await leave(request.agent_id, request.room_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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
    read_committed_config_and_runtime(api_request, lambda _config_data: None)
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
