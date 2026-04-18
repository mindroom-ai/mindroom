"""Matrix client facade plus session and room-administration helpers."""

from __future__ import annotations

import json
import ssl as ssl_module
from collections.abc import AsyncGenerator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    cached_room,  # noqa: F401
    edit_message_result,
    send_file_message,
    send_message_result,
)
from mindroom.matrix.client_thread_history import (
    RoomThreadsPageError,
    get_room_threads_page,
)
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    replace_visible_message,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE

if TYPE_CHECKING:
    from mindroom.config.matrix import RoomDirectoryVisibility, RoomJoinRule

logger = get_logger(__name__)

_PERMANENT_MATRIX_STARTUP_ERROR_CODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_USER_DEACTIVATED",
        "M_UNKNOWN_TOKEN",
        "M_INVALID_USERNAME",
    },
)
_POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
_THREAD_TAGS_POWER_LEVEL = 0
_DEFAULT_STATE_EVENT_POWER_LEVEL = 50


class PermanentMatrixStartupError(ValueError):
    """Raised for Matrix startup failures that should not be retried."""


def _require_runtime_paths_arg(runtime_paths: object) -> RuntimePaths:
    """Reject stale positional call shapes with a clear error."""
    if isinstance(runtime_paths, RuntimePaths):
        return runtime_paths
    msg = (
        "matrix_client() requires RuntimePaths as its second argument. "
        "Call matrix_client(homeserver, runtime_paths, user_id=...)"
    )
    raise TypeError(msg)


def matrix_startup_error(
    message: str,
    *,
    response: object | None = None,
    permanent: bool = False,
) -> ValueError:
    """Return the appropriate startup exception type for a Matrix failure."""
    if permanent:
        return PermanentMatrixStartupError(message)
    if isinstance(response, nio.ErrorResponse) and response.status_code in _PERMANENT_MATRIX_STARTUP_ERROR_CODES:
        return PermanentMatrixStartupError(message)
    return ValueError(message)


def _maybe_ssl_context(homeserver: str, runtime_paths: RuntimePaths) -> ssl_module.SSLContext | None:
    if homeserver.startswith("https://"):
        if not runtime_matrix_ssl_verify(runtime_paths=runtime_paths):
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            ssl_context = ssl_module.create_default_context()
        return ssl_context
    return None


def _create_matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
    store_path: str | None = None,
) -> nio.AsyncClient:
    """Create a Matrix client with consistent configuration."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    ssl_context = _maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    if store_path is None and user_id:
        safe_user_id = user_id.replace(":", "_").replace("@", "")
        store_path = str(encryption_keys_dir(runtime_paths=runtime_paths) / safe_user_id)
        Path(store_path).mkdir(parents=True, exist_ok=True)

    client = nio.AsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        ssl=ssl_context,  # ty: ignore[invalid-argument-type]
    )
    if user_id:
        client.user_id = user_id
    if access_token:
        client.access_token = access_token
    return client


@asynccontextmanager
async def matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
) -> AsyncGenerator[nio.AsyncClient, None]:
    """Context manager for Matrix client that ensures proper cleanup."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
    try:
        yield client
    finally:
        await client.close()


async def login(
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Login to Matrix and return an authenticated client."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        logger.info("matrix_login_succeeded", user_id=user_id)
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


async def restore_login(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Restore one authenticated Matrix session without creating a new device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
    client.restore_login(user_id, device_id, access_token)

    response = await client.whoami()
    if isinstance(response, nio.WhoamiResponse):
        logger.info("matrix_login_restored", user_id=user_id, device_id=device_id)
        return client

    await client.close()
    msg = f"Failed to restore Matrix login for {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


async def invite_to_room(
    client: nio.AsyncClient,
    room_id: str,
    user_id: str,
) -> bool:
    """Invite a user to a room."""
    response = await client.room_invite(room_id, user_id)
    if isinstance(response, nio.RoomInviteResponse):
        logger.info("matrix_room_invited", room_id=room_id, user_id=user_id)
        return True
    logger.error("matrix_room_invite_failed", room_id=room_id, user_id=user_id, error=str(response))
    return False


async def create_room(
    client: nio.AsyncClient,
    name: str,
    alias: str | None = None,
    topic: str | None = None,
    power_users: list[str] | None = None,
) -> str | None:
    """Create a new Matrix room."""
    room_config: dict[str, Any] = {"name": name}
    if alias:
        room_config["alias"] = alias
    if topic:
        room_config["topic"] = topic

    power_level_content: dict[str, Any] = {
        "state_default": _DEFAULT_STATE_EVENT_POWER_LEVEL,
        "events": {
            THREAD_TAGS_EVENT_TYPE: _THREAD_TAGS_POWER_LEVEL,
        },
    }
    users: dict[str, int] = {}
    if power_users:
        users.update(dict.fromkeys(power_users, 50))
    if client.user_id:
        users[client.user_id] = 100
    if users:
        power_level_content["users"] = users
    room_config["initial_state"] = [{"type": _POWER_LEVELS_EVENT_TYPE, "content": power_level_content}]

    response = await client.room_create(**room_config)
    if isinstance(response, nio.RoomCreateResponse):
        logger.info("matrix_room_created", room_id=str(response.room_id), name=name)
        room_id = str(response.room_id)
        if power_users:
            for user_id in power_users:
                if user_id != client.user_id:
                    await invite_to_room(client, room_id, user_id)
        return room_id
    logger.error("matrix_room_creation_failed", name=name, error=str(response))
    return None


def _with_thread_tags_power_level(power_levels_content: dict[str, Any]) -> dict[str, Any]:
    """Return power-level content with the thread-tags override applied."""
    next_content = dict(power_levels_content)
    existing_events = power_levels_content.get("events")
    next_events = dict(existing_events) if isinstance(existing_events, dict) else {}
    next_events[THREAD_TAGS_EVENT_TYPE] = _THREAD_TAGS_POWER_LEVEL
    next_content["events"] = next_events
    return next_content


async def ensure_thread_tags_power_level(
    client: nio.AsyncClient,
    room_id: str,
) -> bool:
    """Ensure managed rooms allow PL0 users to send the thread-tags state event."""
    current_response = await client.room_get_state_event(room_id, _POWER_LEVELS_EVENT_TYPE)
    if not isinstance(current_response, nio.RoomGetStateEventResponse):
        logger.error(
            "Failed to read room power levels for thread tags reconciliation",
            room_id=room_id,
            error=_describe_matrix_response_error(current_response),
        )
        return False
    if not isinstance(current_response.content, dict):
        logger.error(
            "Room power levels state has unexpected content shape",
            room_id=room_id,
            content=current_response.content,
        )
        return False
    current_content = current_response.content

    desired_content = _with_thread_tags_power_level(current_content)
    if desired_content == current_content:
        logger.debug(
            "Thread tags power level already configured",
            room_id=room_id,
            event_type=THREAD_TAGS_EVENT_TYPE,
            power_level=_THREAD_TAGS_POWER_LEVEL,
        )
        return True

    response = await client.room_put_state(
        room_id=room_id,
        event_type=_POWER_LEVELS_EVENT_TYPE,
        content=desired_content,
    )
    if isinstance(response, nio.RoomPutStateResponse):
        logger.info(
            "Updated room power levels for thread tags",
            room_id=room_id,
            event_type=THREAD_TAGS_EVENT_TYPE,
            power_level=_THREAD_TAGS_POWER_LEVEL,
        )
        return True

    logger.error(
        "Failed to update room power levels for thread tags",
        room_id=room_id,
        error=_describe_matrix_response_error(response),
        hint="Ensure the service account is joined and can update m.room.power_levels.",
    )
    return False


async def create_space(
    client: nio.AsyncClient,
    name: str,
    alias: str | None = None,
    topic: str | None = None,
) -> str | None:
    """Create a private Matrix Space."""
    room_config: dict[str, Any] = {
        "name": name,
        "space": True,
        "preset": nio.RoomPreset.private_chat,
    }
    if alias:
        room_config["alias"] = alias
    if topic:
        room_config["topic"] = topic

    response = await client.room_create(**room_config)
    if isinstance(response, nio.RoomCreateResponse):
        logger.info("matrix_space_created", room_id=str(response.room_id), name=name)
        return str(response.room_id)

    logger.error("matrix_space_creation_failed", name=name, error=str(response))
    return None


def _describe_matrix_response_error(response: object) -> str:
    """Convert a Matrix response object into a concise error string."""
    if isinstance(response, nio.ErrorResponse):
        if response.status_code and response.message:
            return f"{response.status_code}: {response.message}"
        if response.status_code:
            return str(response.status_code)
        if response.message:
            return str(response.message)
    return str(response)


async def _get_room_join_rule(client: nio.AsyncClient, room_id: str) -> str | None:
    """Read the current join rule from room state."""
    response = await client.room_get_state_event(room_id, "m.room.join_rules")
    if isinstance(response, nio.RoomGetStateEventResponse):
        join_rule = response.content.get("join_rule")
        if isinstance(join_rule, str):
            return join_rule
        logger.warning(
            "Room join rule state missing expected 'join_rule' field",
            room_id=room_id,
            content=response.content,
        )
        return None

    logger.warning(
        "Failed to read room join rule",
        room_id=room_id,
        error=_describe_matrix_response_error(response),
    )
    return None


async def _set_room_join_rule(
    client: nio.AsyncClient,
    room_id: str,
    join_rule: RoomJoinRule,
) -> bool:
    """Write the room join rule state event."""
    response = await client.room_put_state(
        room_id=room_id,
        event_type="m.room.join_rules",
        content={"join_rule": join_rule},
    )
    if isinstance(response, nio.RoomPutStateResponse):
        logger.info("Updated room join rule", room_id=room_id, join_rule=join_rule)
        return True

    logger.warning(
        "Failed to set room join rule",
        room_id=room_id,
        join_rule=join_rule,
        error=_describe_matrix_response_error(response),
        hint=(
            "Ensure the service account is joined to the room and has enough power "
            "to send m.room.join_rules state events."
        ),
    )
    return False


async def ensure_room_join_rule(
    client: nio.AsyncClient,
    room_id: str,
    target_join_rule: RoomJoinRule,
) -> bool:
    """Ensure a room has the desired join rule."""
    current_join_rule = await _get_room_join_rule(client, room_id)
    if current_join_rule == target_join_rule:
        logger.debug("Room join rule already configured", room_id=room_id, join_rule=target_join_rule)
        return True
    return await _set_room_join_rule(client, room_id, target_join_rule)


async def _get_room_directory_visibility(client: nio.AsyncClient, room_id: str) -> str | None:
    """Read the current room directory visibility."""
    response = await client.room_get_visibility(room_id)
    if isinstance(response, nio.RoomGetVisibilityResponse):
        return str(response.visibility)

    logger.warning(
        "Failed to read room directory visibility",
        room_id=room_id,
        error=_describe_matrix_response_error(response),
    )
    return None


async def _set_room_directory_visibility(
    client: nio.AsyncClient,
    room_id: str,
    visibility: RoomDirectoryVisibility,
) -> bool:
    """Set room visibility in the server room directory."""
    if not client.access_token:
        logger.warning(
            "Cannot set room directory visibility without access token",
            room_id=room_id,
            visibility=visibility,
        )
        return False

    _method, path = nio.Api.room_get_visibility(room_id)
    payload = json.dumps({"visibility": visibility})
    response = await client.send(
        "PUT",
        path,
        data=payload,
        headers={
            "Authorization": f"Bearer {client.access_token}",
            "Content-Type": "application/json",
        },
    )
    if 200 <= response.status < 300:
        response.release()
        logger.info("Updated room directory visibility", room_id=room_id, visibility=visibility)
        return True

    error_text = await response.text()
    response.release()
    hint = (
        "Ensure the service account is a room moderator/admin; Synapse requires sufficient "
        "power in the room to edit directory entries."
        if response.status == 403
        else "Check homeserver logs and Matrix API response for details."
    )
    logger.warning(
        "Failed to set room directory visibility",
        room_id=room_id,
        visibility=visibility,
        http_status=response.status,
        error=error_text,
        hint=hint,
    )
    return False


async def ensure_room_directory_visibility(
    client: nio.AsyncClient,
    room_id: str,
    target_visibility: RoomDirectoryVisibility,
) -> bool:
    """Ensure a room has the desired directory visibility."""
    current_visibility = await _get_room_directory_visibility(client, room_id)
    if current_visibility == target_visibility:
        logger.debug("Room directory visibility already configured", room_id=room_id, visibility=target_visibility)
        return True
    return await _set_room_directory_visibility(client, room_id, target_visibility)


async def ensure_room_name(
    client: nio.AsyncClient,
    room_id: str,
    name: str,
) -> bool:
    """Ensure a room or Space has the desired display name."""
    current_response = await client.room_get_state_event(room_id, "m.room.name")
    if isinstance(current_response, nio.RoomGetStateEventResponse) and current_response.content.get("name") == name:
        logger.debug("Room name already configured", room_id=room_id, name=name)
        return True

    response = await client.room_put_state(
        room_id=room_id,
        event_type="m.room.name",
        content={"name": name},
    )
    if isinstance(response, nio.RoomPutStateResponse):
        logger.info("Updated room name", room_id=room_id, name=name)
        return True

    logger.error(
        "Failed to update room name",
        room_id=room_id,
        name=name,
        error=_describe_matrix_response_error(response),
    )
    return False


async def add_room_to_space(
    client: nio.AsyncClient,
    space_id: str,
    room_id: str,
    via_server_name: str,
    *,
    suggested: bool = True,
) -> bool:
    """Ensure a room is linked as a child of a root Space."""
    desired_content = {
        "via": [via_server_name],
        "suggested": suggested,
    }

    current_response = await client.room_get_state_event(space_id, "m.space.child", room_id)
    if isinstance(current_response, nio.RoomGetStateEventResponse) and current_response.content == desired_content:
        logger.debug("Room already linked under root space", space_id=space_id, room_id=room_id)
        return True

    response = await client.room_put_state(
        room_id=space_id,
        event_type="m.space.child",
        content=desired_content,
        state_key=room_id,
    )
    if isinstance(response, nio.RoomPutStateResponse):
        logger.info("Linked room under root space", space_id=space_id, room_id=room_id)
        return True

    logger.error(
        "Failed to link room under root space",
        space_id=space_id,
        room_id=room_id,
        error=_describe_matrix_response_error(response),
    )
    return False


async def join_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Join a Matrix room."""
    response = await client.join(room_id)
    if isinstance(response, nio.JoinResponse):
        rooms = client.rooms
        if isinstance(rooms, MutableMapping) and response.room_id not in rooms:
            rooms[response.room_id] = nio.MatrixRoom(
                room_id=response.room_id,
                own_user_id=client.user_id or "",
            )
        logger.info("matrix_room_joined", room_id=room_id)
        return True
    logger.warning("matrix_room_join_failed", room_id=room_id, error=str(response))
    return False


async def get_room_members(client: nio.AsyncClient, room_id: str) -> set[str]:
    """Get the current members of a room."""
    response = await client.joined_members(room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return {member.user_id for member in response.members}
    logger.warning("matrix_room_members_fetch_failed", room_id=room_id)
    return set()


async def get_joined_rooms(client: nio.AsyncClient) -> list[str] | None:
    """Get all rooms the client has joined."""
    response = await client.joined_rooms()
    if isinstance(response, nio.JoinedRoomsResponse):
        return list(response.rooms)
    logger.error("matrix_joined_rooms_fetch_failed", error=str(response))
    return None


async def get_room_name(client: nio.AsyncClient, room_id: str) -> str:
    """Get the display name of a Matrix room."""
    response = await client.room_get_state_event(room_id, "m.room.name")
    if isinstance(response, nio.RoomGetStateEventResponse) and response.content.get("name"):
        return str(response.content["name"])

    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return "Unnamed Room"

    for event in response.events:
        if event.get("type") == "m.room.name" and event.get("content", {}).get("name"):
            return str(event["content"]["name"])

    members = [
        event.get("content", {}).get("displayname", event.get("state_key", ""))
        for event in response.events
        if event.get("type") == "m.room.member"
        and event.get("content", {}).get("membership") == "join"
        and event.get("state_key") != client.user_id
    ]

    if len(members) == 1:
        return f"DM with {members[0]}"
    if members:
        return f"Room with {', '.join(members[:3])}" + (" and others" if len(members) > 3 else "")
    return "Unnamed Room"


async def leave_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Leave a Matrix room."""
    response = await client.room_leave(room_id)
    if isinstance(response, nio.RoomLeaveResponse):
        logger.info("matrix_room_left", room_id=room_id)
        return True
    logger.error("matrix_room_leave_failed", room_id=room_id, error=str(response))
    return False


__all__ = [
    "PermanentMatrixStartupError",
    "ResolvedVisibleMessage",
    "RoomThreadsPageError",
    "add_room_to_space",
    "create_room",
    "edit_message_result",
    "get_joined_rooms",
    "get_room_members",
    "get_room_name",
    "get_room_threads_page",
    "invite_to_room",
    "join_room",
    "leave_room",
    "replace_visible_message",
    "send_file_message",
    "send_message_result",
]
