"""API endpoints for Matrix operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import nio
from aiohttp import ClientError
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mindroom.api.config_lifecycle import app_state, read_committed_config_and_runtime, read_committed_runtime_config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_name
from mindroom.matrix.media import MatrixMediaUpstreamError, fetch_matrix_thumbnail, matrix_profile_avatar_uri
from mindroom.matrix.rooms import filter_non_dm_rooms, rejected_managed_rooms
from mindroom.matrix.state import resolve_room_aliases
from mindroom.matrix.users import create_agent_http_client

logger = get_logger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])
_AVATAR_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


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
    # Managed room aliases the runtime refused to adopt or manage, mapped to the reason.
    rejected_managed_rooms: dict[str, str] = Field(default_factory=dict)


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


def _avatar_response(thumbnail: tuple[bytes, str]) -> Response:
    body, content_type = thumbnail
    return Response(body, media_type=content_type, headers=_AVATAR_HEADERS)


def _room_avatar_target(
    config: Config,
    requested_room: str,
    runtime_paths: constants.RuntimePaths,
) -> tuple[str, tuple[str, ...]]:
    configured_rooms = sorted(config.get_all_configured_rooms())
    resolved_rooms = resolve_room_aliases(configured_rooms, runtime_paths=runtime_paths)
    matching_room_ids = {
        resolved
        for configured, resolved in zip(configured_rooms, resolved_rooms, strict=True)
        if requested_room in {configured, resolved}
    }
    if not matching_room_ids:
        raise HTTPException(404, "Room avatar is not available", headers=_AVATAR_HEADERS)

    room_id = next(iter(matching_room_ids))
    if not room_id.startswith("!"):
        raise HTTPException(404, "Room avatar is not available", headers=_AVATAR_HEADERS)

    candidates = [
        entity_id
        for entity_id in _get_runtime_matrix_entities(config)
        if room_id
        in resolve_room_aliases(
            get_rooms_for_entity(entity_id, config),
            runtime_paths=runtime_paths,
        )
    ]
    candidates.append(ROUTER_AGENT_NAME)
    return room_id, tuple(dict.fromkeys(candidates))


@router.get("/agents/{agent_id}/avatar")
async def get_agent_avatar(agent_id: str, request: Request) -> Response:
    """Serve a configured Matrix entity's current thumbnail."""
    config, runtime_paths = read_committed_runtime_config(request)
    _get_runtime_matrix_entity(config, agent_id)
    try:
        client = create_agent_http_client(agent_id, runtime_paths)
    except ValueError as exc:
        raise HTTPException(404, "Avatar is not available", headers=_AVATAR_HEADERS) from exc
    try:
        async with asyncio.timeout(5):
            profile = await client.get_profile(client.user_id)
            avatar_url = matrix_profile_avatar_uri(profile)
            if not avatar_url:
                raise HTTPException(404, "Avatar is not available", headers=_AVATAR_HEADERS)
            thumbnail = await fetch_matrix_thumbnail(client, avatar_url)
            if thumbnail is None:
                raise HTTPException(404, "Avatar is not available", headers=_AVATAR_HEADERS)
            return _avatar_response(thumbnail)
    except (ClientError, MatrixMediaUpstreamError, TimeoutError) as exc:
        raise HTTPException(502, "Avatar is temporarily unavailable", headers=_AVATAR_HEADERS) from exc
    finally:
        await client.close()


@router.get("/rooms/avatar")
async def get_room_avatar(room_id: str, request: Request) -> Response:
    """Serve the current thumbnail for one configured room reference."""
    config, runtime_paths = read_committed_runtime_config(request)
    resolved_room_id, candidates = _room_avatar_target(config, room_id, runtime_paths)
    saw_upstream_error = False
    try:
        async with asyncio.timeout(5):
            for candidate in candidates:
                try:
                    client = create_agent_http_client(candidate, runtime_paths)
                except ValueError:
                    continue
                try:
                    state = await client.room_get_state_event(resolved_room_id, "m.room.avatar")
                    if isinstance(state, nio.RoomGetStateEventResponse):
                        if not isinstance(state.content, dict):
                            saw_upstream_error = True
                            continue
                        thumbnail = await fetch_matrix_thumbnail(client, state.content.get("url"))
                        if thumbnail is None:
                            raise HTTPException(404, "Room avatar is not available", headers=_AVATAR_HEADERS)
                        return _avatar_response(thumbnail)
                    if isinstance(state, nio.RoomGetStateEventError) and state.status_code == "M_NOT_FOUND":
                        raise HTTPException(404, "Room avatar is not available", headers=_AVATAR_HEADERS)
                    if not isinstance(state, nio.RoomGetStateEventError) or state.status_code != "M_FORBIDDEN":
                        saw_upstream_error = True
                finally:
                    await client.close()
    except (ClientError, MatrixMediaUpstreamError, TimeoutError) as exc:
        raise HTTPException(502, "Room avatar is temporarily unavailable", headers=_AVATAR_HEADERS) from exc

    if saw_upstream_error:
        raise HTTPException(502, "Room avatar is temporarily unavailable", headers=_AVATAR_HEADERS)
    raise HTTPException(404, "Room avatar is not available", headers=_AVATAR_HEADERS)


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

    return AllAgentsRoomsResponse(agents=agents_rooms, rejected_managed_rooms=rejected_managed_rooms())


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
