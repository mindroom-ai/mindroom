"""Matrix client operations and utilities."""

import asyncio
import io
import json
import mimetypes
import ssl as ssl_module
from collections.abc import AsyncGenerator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import nio
from aiohttp import ClientError
from nio import crypto
from nio.api import RelationshipType
from nio.responses import RoomThreadsResponse

from mindroom.config.main import Config
from mindroom.config.matrix import RoomDirectoryVisibility, RoomJoinRule
from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.event_cache import EventCache, normalize_event_source_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import (
    extract_and_resolve_message,
    extract_edit_body,
    resolve_event_source_content,
    visible_body_from_event_source,
)
from mindroom.matrix.room_cache import cached_room
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE

logger = get_logger(__name__)
_VISIBLE_ROOM_MESSAGE_EVENT_TYPES = (nio.RoomMessageText, nio.RoomMessageNotice)

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


class _ThreadHistoryFastPathUnavailableError(RuntimeError):
    """Raised when the lightweight relations snapshot cannot be used."""


_EVENT_CACHE_BY_CLIENT: WeakKeyDictionary[nio.AsyncClient, EventCache] = WeakKeyDictionary()


def attach_event_cache(client: nio.AsyncClient, event_cache: EventCache) -> None:
    """Attach one event cache instance to a Matrix client."""
    _EVENT_CACHE_BY_CLIENT[client] = event_cache


def detach_event_cache(client: nio.AsyncClient) -> None:
    """Detach any event cache associated with a Matrix client."""
    _EVENT_CACHE_BY_CLIENT.pop(client, None)


def get_event_cache(client: nio.AsyncClient) -> EventCache | None:
    """Return the event cache associated with a Matrix client, if any."""
    return _EVENT_CACHE_BY_CLIENT.get(client)


@dataclass(slots=True)
class ResolvedVisibleMessage:
    """Canonical visible message state used during history reconstruction."""

    sender: str
    body: str
    timestamp: int
    event_id: str
    content: dict[str, Any]
    thread_id: str | None
    latest_event_id: str
    stream_status: str | None = None

    @classmethod
    def from_message_data(
        cls,
        message_data: dict[str, Any],
        *,
        thread_id: str | None,
        latest_event_id: str,
    ) -> "ResolvedVisibleMessage":
        """Build a resolved visible message from extracted message data."""
        message = cls(
            sender=message_data["sender"],
            body=message_data["body"],
            timestamp=message_data["timestamp"],
            event_id=message_data["event_id"],
            content=message_data["content"],
            thread_id=thread_id,
            latest_event_id=latest_event_id,
        )
        message.refresh_stream_status()
        return message

    @classmethod
    def synthetic(
        cls,
        *,
        sender: str,
        body: str,
        event_id: str,
        timestamp: int = 0,
        content: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> "ResolvedVisibleMessage":
        """Build a synthetic visible message for non-Matrix history inputs."""
        message = cls(
            sender=sender,
            body=body,
            timestamp=timestamp,
            event_id=event_id,
            content=content or {"body": body},
            thread_id=thread_id,
            latest_event_id=event_id,
        )
        message.refresh_stream_status()
        return message

    def refresh_stream_status(self) -> None:
        """Refresh normalized stream status from message content."""
        self.stream_status = _stream_status_from_content(self.content)

    def apply_edit(
        self,
        *,
        body: str,
        timestamp: int,
        latest_event_id: str,
        thread_id: str | None,
        content: dict[str, Any] | None,
    ) -> None:
        """Apply the newest visible edit state to this message."""
        self.body = body
        self.timestamp = timestamp
        self.latest_event_id = latest_event_id
        if thread_id is not None:
            self.thread_id = thread_id
        if content is not None:
            self.content = content
        self.refresh_stream_status()

    @property
    def visible_event_id(self) -> str:
        """Return the event ID for the currently visible event state."""
        return self.latest_event_id

    @property
    def reply_to_event_id(self) -> str | None:
        """Return the explicit reply target encoded on the visible content."""
        return _reply_to_event_id_from_content(self.content)

    def to_dict(self) -> dict[str, Any]:
        """Convert the resolved message back to the public dictionary shape."""
        message_data = {
            "sender": self.sender,
            "body": self.body,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "content": self.content,
            "thread_id": self.thread_id,
            "latest_event_id": self.latest_event_id,
        }
        msgtype = self.content.get("msgtype")
        if isinstance(msgtype, str) and msgtype != "m.text":
            message_data["msgtype"] = msgtype
        if self.stream_status is not None:
            message_data["stream_status"] = self.stream_status
        return message_data


def _reply_to_event_id_from_content(content: Mapping[str, Any] | None) -> str | None:
    """Return the explicit reply target encoded on one visible content payload."""
    if content is None:
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, Mapping):
        return None
    in_reply_to = relates_to.get("m.in_reply_to")
    if not isinstance(in_reply_to, Mapping):
        return None
    reply_to_event_id = in_reply_to.get("event_id")
    return reply_to_event_id if isinstance(reply_to_event_id, str) else None


def _thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    return thread_history_result(history, is_full_history=is_full_history)


def replace_visible_message(
    message: ResolvedVisibleMessage,
    *,
    sender: str | None = None,
    body: str | None = None,
) -> ResolvedVisibleMessage:
    """Return one visible-message copy while keeping body/content coherent."""
    updated_content: dict[str, Any] | None = None
    if body is not None:
        content = message.content
        updated_content = dict(content)
        updated_content["body"] = body

    updates: dict[str, str | dict[str, Any]] = {}
    if sender is not None:
        updates["sender"] = sender
    if body is not None:
        updates["body"] = body
    if updated_content is not None:
        updates["content"] = updated_content
    return replace(message, **updates)


class PermanentMatrixStartupError(ValueError):
    """Raised for Matrix startup failures that should not be retried."""


class RoomThreadsPageError(ValueError):
    """Raised when a single /threads page request fails."""

    def __init__(
        self,
        *,
        response: str,
        errcode: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(response)
        self.response = response
        self.errcode = errcode
        self.retry_after_ms = retry_after_ms


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
            # Create context that disables verification for dev/self-signed certs
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            # Use default context with proper verification
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
    """Create a Matrix client with consistent configuration.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: Optional user ID for authenticated client
        access_token: Optional access token for authenticated client
        store_path: Optional path for encryption key storage (defaults to .nio_store/<user_id>)
        runtime_paths: Explicit runtime context for SSL and storage resolution

    Returns:
        nio.AsyncClient: Configured Matrix client instance

    """
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    ssl_context = _maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    # Default store path for encryption support
    if store_path is None and user_id:
        safe_user_id = user_id.replace(":", "_").replace("@", "")
        store_path = str(encryption_keys_dir(runtime_paths=runtime_paths) / safe_user_id)
        # Ensure the directory exists
        Path(store_path).mkdir(parents=True, exist_ok=True)

    client = nio.AsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        ssl=ssl_context,  # ty: ignore[invalid-argument-type]  # nio accepts SSLContext but types say bool
    )

    # Manually set user_id due to matrix-nio bug where constructor parameter doesn't work
    # See: https://github.com/matrix-nio/matrix-nio/issues/492
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
    """Context manager for Matrix client that ensures proper cleanup.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: Optional user ID for authenticated client
        access_token: Optional access token for authenticated client
        runtime_paths: Explicit runtime context for SSL and storage resolution

    Yields:
        nio.AsyncClient: The Matrix client instance

    Example:
        async with matrix_client("http://localhost:8008", runtime_paths, user_id="@user:localhost") as client:
            response = await client.login(password="secret")

    """
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
    """Login to Matrix and return authenticated client.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: The full Matrix user ID (e.g., @user:localhost)
        password: The user's password
        runtime_paths: Explicit runtime context for SSL and storage resolution

    Returns:
        Authenticated AsyncClient instance

    Raises:
        ValueError: If login fails

    """
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        logger.info(f"Successfully logged in: {user_id}")
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


async def invite_to_room(
    client: nio.AsyncClient,
    room_id: str,
    user_id: str,
) -> bool:
    """Invite a user to a room.

    Args:
        client: Authenticated Matrix client
        room_id: The room to invite to
        user_id: The user to invite

    Returns:
        True if successful, False otherwise

    """
    response = await client.room_invite(room_id, user_id)
    if isinstance(response, nio.RoomInviteResponse):
        logger.info(f"Invited {user_id} to room {room_id}")
        return True
    logger.error(f"Failed to invite {user_id} to room {room_id}: {response}")
    return False


async def create_room(
    client: nio.AsyncClient,
    name: str,
    alias: str | None = None,
    topic: str | None = None,
    power_users: list[str] | None = None,
) -> str | None:
    """Create a new Matrix room.

    Args:
        client: Authenticated Matrix client
        name: Room name
        alias: Optional room alias (without # and domain)
        topic: Optional room topic
        power_users: Optional list of user IDs to grant power level 50

    Returns:
        Room ID if successful, None otherwise

    """
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
        logger.info(f"Created room: {name} ({response.room_id})")
        room_id = str(response.room_id)

        # Invite power users to the room
        if power_users:
            for user_id in power_users:
                # Skip inviting ourselves
                if user_id != client.user_id:
                    await invite_to_room(client, room_id, user_id)

        return room_id
    logger.error(f"Failed to create room {name}: {response}")
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
    # Always fetch fresh power levels from the homeserver because the content is
    # used as the base for a write-back. nio's cached PowerLevels can retain
    # stale user/event overrides that were already removed server-side, and
    # writing those back would silently restore revoked permissions.
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
        logger.info(f"Created space: {name} ({response.room_id})")
        return str(response.room_id)

    logger.error(f"Failed to create space {name}: {response}")
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


def _room_threads_page_error_from_response(response: object) -> RoomThreadsPageError:
    """Preserve nio response details for /threads pagination failures."""
    if isinstance(response, nio.ErrorResponse):
        return RoomThreadsPageError(
            response=str(response),
            errcode=response.status_code,
            retry_after_ms=response.retry_after_ms,
        )
    return RoomThreadsPageError(response=str(response))


def _room_threads_page_error_from_exception(exc: BaseException) -> RoomThreadsPageError:
    """Normalize transport failures into the same structured /threads error."""
    detail = str(exc)
    response = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return RoomThreadsPageError(response=response)


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


async def _create_dm_room(
    client: nio.AsyncClient,
    invite_user_ids: list[str],
    name: str | None = None,
) -> str | None:
    """Create a Direct Message room with specific users.

    Args:
        client: Authenticated Matrix client
        invite_user_ids: List of user IDs to invite to the DM
        name: Optional room name (defaults to "Direct Message")

    Returns:
        Room ID if successful, None otherwise

    """
    room_config: dict[str, Any] = {
        "preset": "trusted_private_chat",  # DM preset - no need to invite, they can join
        "is_direct": True,  # Mark as DM
        "invite": invite_user_ids,
    }

    if name:
        room_config["name"] = name

    response = await client.room_create(**room_config)
    if isinstance(response, nio.RoomCreateResponse):
        logger.info(f"Created DM room: {response.room_id}")
        return str(response.room_id)

    logger.error(f"Failed to create DM room: {response}")
    return None


async def join_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Join a Matrix room.

    Args:
        client: Authenticated Matrix client
        room_id: Room ID or alias to join

    Returns:
        True if successful, False otherwise

    """
    response = await client.join(room_id)
    if isinstance(response, nio.JoinResponse):
        logger.info(f"Joined room: {room_id}")
        return True
    logger.warning(f"Could not join room {room_id}: {response}")
    return False


async def get_room_members(client: nio.AsyncClient, room_id: str) -> set[str]:
    """Get the current members of a room.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID

    Returns:
        Set of user IDs in the room

    """
    response = await client.joined_members(room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return {member.user_id for member in response.members}
    logger.warning(f"⚠️ Could not check members for room {room_id}")
    return set()


async def get_joined_rooms(client: nio.AsyncClient) -> list[str] | None:
    """Get all rooms the client has joined.

    Args:
        client: Authenticated Matrix client

    Returns:
        List of room IDs the client has joined, or None if the request failed

    """
    response = await client.joined_rooms()
    if isinstance(response, nio.JoinedRoomsResponse):
        return list(response.rooms)
    logger.error(f"Failed to get joined rooms: {response}")
    return None


async def get_room_name(client: nio.AsyncClient, room_id: str) -> str:
    """Get the display name of a Matrix room.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to get the name for

    Returns:
        Room name if found, fallback name for DM/unnamed rooms

    """
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
        room_name = f"DM with {members[0]}"
    elif members:
        room_name = f"Room with {', '.join(members[:3])}" + (" and others" if len(members) > 3 else "")
    else:
        room_name = "Unnamed Room"
    return room_name


async def leave_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Leave a Matrix room.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to leave

    Returns:
        True if successfully left the room, False otherwise

    """
    response = await client.room_leave(room_id)
    if isinstance(response, nio.RoomLeaveResponse):
        logger.info(f"Left room {room_id}")
        return True
    logger.error(f"Failed to leave room {room_id}: {response}")
    return False


async def send_message(client: nio.AsyncClient, room_id: str, content: dict[str, Any]) -> str | None:
    """Send a message to a Matrix room.

    Automatically handles large messages that exceed the Matrix event size limit
    by uploading the full content as MXC and sending a maximum-size preview.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to send the message to
        content: The message content dictionary

    Returns:
        The event ID of the sent message, or None if sending failed

    """
    # Handle large messages if needed
    content = await prepare_large_message(client, room_id, content)

    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )
    if isinstance(response, nio.RoomSendResponse):
        logger.debug(f"Sent message to {room_id}: {response.event_id}")
        return str(response.event_id)
    logger.error(f"Failed to send message to {room_id}: {response}")
    return None


def _guess_mimetype(file_path: Path) -> str:
    guessed_mimetype, _ = mimetypes.guess_type(file_path.name)
    return guessed_mimetype or "application/octet-stream"


async def _upload_file_as_mxc(
    client: nio.AsyncClient,
    room_id: str,
    file_path: Path,
    *,
    mimetype: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload a local file as MXC, encrypting payloads in encrypted rooms."""
    try:
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
    except OSError:
        logger.exception("Failed to read file before upload", path=str(file_path))
        return None, None

    info: dict[str, Any] = {"size": len(file_bytes), "mimetype": mimetype}
    room = cached_room(client, room_id)
    if room is None:
        logger.error("Cannot determine encryption state for unknown room", room_id=room_id)
        return None, None
    room_encrypted = bool(room.encrypted)
    upload_bytes = file_bytes
    encrypted_file_payload: dict[str, Any] | None = None
    upload_mimetype = mimetype
    upload_name = file_path.name

    if room_encrypted:
        try:
            encrypted_bytes, encryption_keys = crypto.attachments.encrypt_attachment(file_bytes)
        except Exception:
            logger.exception("Failed to encrypt file attachment", path=str(file_path))
            return None, None
        upload_bytes = encrypted_bytes
        upload_mimetype = "application/octet-stream"
        upload_name = f"{file_path.name}.enc"
        encrypted_file_payload = {
            "url": "",
            "key": encryption_keys["key"],
            "iv": encryption_keys["iv"],
            "hashes": encryption_keys["hashes"],
            "v": "v2",
            "mimetype": mimetype,
            "size": len(file_bytes),
        }

    _upload_payload = upload_bytes  # bind eagerly so the closure is refactor-safe

    def data_provider(_monitor: object, _data: object) -> io.BytesIO:
        return io.BytesIO(_upload_payload)

    try:
        upload_response = await client.upload(
            data_provider=data_provider,
            content_type=upload_mimetype,
            filename=upload_name,
            filesize=len(upload_bytes),
        )
    except Exception:
        logger.exception("Failed uploading Matrix file", path=str(file_path))
        return None, None

    upload_result = upload_response[0] if isinstance(upload_response, tuple) else upload_response

    if not isinstance(upload_result, nio.UploadResponse) or not upload_result.content_uri:
        logger.error("Failed file upload response", path=str(file_path), response=str(upload_result))
        return None, None

    mxc_uri = str(upload_result.content_uri)
    upload_payload: dict[str, Any] = {"info": info}
    if encrypted_file_payload is not None:
        encrypted_file_payload["url"] = mxc_uri
        upload_payload["file"] = encrypted_file_payload
    return mxc_uri, upload_payload


def _msgtype_for_mimetype(mimetype: str) -> str:
    """Return the Matrix msgtype appropriate for the given MIME type."""
    major = mimetype.split("/", 1)[0]
    if major == "image":
        return "m.image"
    if major == "video":
        return "m.video"
    if major == "audio":
        return "m.audio"
    return "m.file"


async def send_file_message(
    client: nio.AsyncClient,
    room_id: str,
    file_path: str | Path,
    *,
    thread_id: str | None = None,
    caption: str | None = None,
) -> str | None:
    """Upload a file and send it with the appropriate Matrix message type."""
    resolved_path = Path(file_path).expanduser().resolve()
    if not resolved_path.is_file():
        logger.error("Cannot send non-file attachment", path=str(resolved_path))
        return None

    mimetype = _guess_mimetype(resolved_path)
    mxc_uri, upload_payload = await _upload_file_as_mxc(client, room_id, resolved_path, mimetype=mimetype)
    if mxc_uri is None or upload_payload is None:
        return None

    info = upload_payload.get("info")
    if not isinstance(info, dict):
        info = {"size": resolved_path.stat().st_size, "mimetype": mimetype}

    msgtype = _msgtype_for_mimetype(mimetype)
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or resolved_path.name,
        "info": info,
    }
    if msgtype == "m.file":
        content["filename"] = resolved_path.name
    encrypted_file_payload = upload_payload.get("file")
    if isinstance(encrypted_file_payload, dict):
        content["file"] = encrypted_file_payload
    else:
        content["url"] = mxc_uri

    if thread_id:
        latest_thread_event_id = await _latest_thread_event_id(client, room_id, thread_id)
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": latest_thread_event_id},
        }

    return await send_message(client, room_id, content)


def _history_message_sort_key(message: ResolvedVisibleMessage) -> tuple[int, str]:
    """Sort thread history messages by timestamp and event ID."""
    return (message.timestamp, message.event_id)


def _is_room_message_event(event: nio.Event) -> bool:
    """Return whether one nio event is a readable Matrix room message."""
    event_source = event.source if isinstance(event.source, dict) else {}
    return event_source.get("type") == "m.room.message"


def _room_message_fallback_body(event: nio.Event) -> str:
    """Return one best-effort fallback body for a room message event."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        return event.body
    event_source = event.source if isinstance(event.source, dict) else {}
    content = event_source.get("content")
    if isinstance(content, dict):
        body = content.get("body")
        if isinstance(body, str):
            return body
    return ""


def _snapshot_message_dict(event: nio.Event) -> ResolvedVisibleMessage:
    """Build one lightweight visible message without hydrating sidecars."""
    event_source = event.source if isinstance(event.source, dict) else {}
    content = event_source.get("content", {})
    normalized_content = content if isinstance(content, dict) else {}
    event_info = EventInfo.from_event(event_source)
    message = ResolvedVisibleMessage.synthetic(
        sender=event.sender,
        body=visible_body_from_event_source(event_source, _room_message_fallback_body(event)),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


async def _fetch_thread_context_via_relations(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Fetch a lightweight thread snapshot from relations when supported."""
    try:
        root_response = await client.room_get_event(room_id, thread_id)
    except Exception as exc:
        msg = f"root lookup failed for {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg) from exc

    if not isinstance(root_response, nio.RoomGetEventResponse):
        msg = f"failed to fetch thread root {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg)

    root_event = root_response.event
    if not _is_room_message_event(root_event):
        msg = f"thread root {thread_id} is not a readable room message"
        raise _ThreadHistoryFastPathUnavailableError(msg)

    thread_events: list[nio.Event] = []
    try:
        async for event in client.room_get_event_relations(
            room_id,
            thread_id,
            rel_type=RelationshipType.thread,
            event_type="m.room.message",
        ):
            if not _is_room_message_event(event):
                continue
            event_info = EventInfo.from_event(event.source)
            if event_info.is_edit:
                continue
            if event_info.thread_id == thread_id:
                thread_events.append(event)
    except Exception as exc:
        msg = f"relations lookup failed for {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg) from exc

    if not thread_events:
        msg = f"no direct thread children returned for {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg)

    snapshot = [_snapshot_message_dict(root_event), *(_snapshot_message_dict(event) for event in thread_events)]
    snapshot.sort(key=_history_message_sort_key)
    return snapshot


def _sort_thread_history_root_first(
    messages: list[ResolvedVisibleMessage],
    *,
    thread_id: str,
) -> None:
    """Keep the thread root first, then order the remaining messages chronologically."""
    messages.sort(key=lambda message: (message.timestamp, message.event_id))
    root_index = next((index for index, message in enumerate(messages) if message.event_id == thread_id), None)
    if root_index not in (None, 0):
        messages.insert(0, messages.pop(root_index))


def _parse_room_message_event(event_source: dict[str, Any]) -> nio.RoomMessageText | nio.RoomMessageNotice | None:
    """Parse one event dict into a readable room message when possible."""
    try:
        parsed_event = nio.Event.parse_event(event_source)
    except Exception:
        return None
    return parsed_event if isinstance(parsed_event, (nio.RoomMessageText, nio.RoomMessageNotice)) else None


def _event_source_for_cache(event: nio.Event) -> dict[str, Any]:
    """Normalize one nio event source for persistent cache storage."""
    server_timestamp = event.server_timestamp
    return normalize_event_source_for_cache(
        event.source,
        event_id=event.event_id if isinstance(event.event_id, str) else None,
        sender=event.sender if isinstance(event.sender, str) else None,
        origin_server_ts=server_timestamp
        if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
        else None,
    )


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def _event_server_timestamp(event: nio.Event) -> int:
    """Return one event timestamp suitable for incremental timeline scans."""
    server_timestamp = event.server_timestamp
    if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool):
        return server_timestamp
    origin_server_ts = event.source.get("origin_server_ts")
    return origin_server_ts if isinstance(origin_server_ts, int) and not isinstance(origin_server_ts, bool) else 0


async def _resolve_thread_history_from_event_sources(
    client: nio.AsyncClient,
    *,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
) -> list[ResolvedVisibleMessage]:
    """Resolve visible thread history from cached raw event sources."""
    parsed_events = [
        parsed_event
        for event_source in event_sources
        if (parsed_event := _parse_room_message_event(event_source)) is not None
    ]
    messages_by_event_id = await resolve_latest_visible_messages(parsed_events, client)
    messages = list(messages_by_event_id.values())
    _sort_thread_history_root_first(messages, thread_id=thread_id)
    return messages


async def _fetch_thread_history_without_cache(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Fetch thread history directly from the homeserver when no cache is active."""
    try:
        return await _fetch_thread_history_via_relations(client, room_id, thread_id)
    except _ThreadHistoryFastPathUnavailableError as exc:
        logger.info(
            "Falling back to room scan for thread history",
            room_id=room_id,
            thread_id=thread_id,
            reason=str(exc),
        )
        return await _fetch_thread_history_via_room_messages(client, room_id, thread_id)


async def _load_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: EventCache,
) -> list[ResolvedVisibleMessage] | None:
    """Return cached thread history when it can be refreshed and resolved safely."""
    try:
        cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    except Exception as exc:
        logger.warning(
            "Event cache read failed; falling back to homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return None

    if cached_event_sources is None:
        return None

    cached_event_ids = {
        event_id
        for event_source in cached_event_sources
        if (event_id := _event_id_from_source(event_source)) is not None
    }
    if not cached_event_ids:
        logger.warning(
            "Cached thread payload was missing event IDs; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
        )
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        return None

    refreshed_event_sources = await _refresh_cached_thread_event_sources(
        client,
        room_id=room_id,
        thread_id=thread_id,
        cached_event_ids=cached_event_ids,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
    )
    return await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=refreshed_event_sources,
    )


async def _refresh_cached_thread_event_sources(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    cached_event_ids: Collection[str],
    event_cache: EventCache,
    cached_event_sources: Sequence[dict[str, Any]],
) -> Sequence[dict[str, Any]]:
    """Apply incremental refreshes to one cached thread payload when possible."""
    try:
        new_event_sources, redactions = await _fetch_incremental_thread_events(
            client,
            room_id,
            thread_id,
            cached_event_ids=cached_event_ids,
        )
        if new_event_sources:
            await event_cache.store_thread_events(room_id, thread_id, new_event_sources)
        for redacted_event_id, redaction_event in redactions:
            await event_cache.redact_event(
                room_id,
                redacted_event_id,
                thread_id=thread_id,
                redaction_event=redaction_event,
            )
        if not new_event_sources and not redactions:
            return cached_event_sources

        refreshed_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    except Exception as exc:
        logger.warning(
            "Incremental thread cache refresh failed; serving stale cache",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return cached_event_sources
    else:
        if refreshed_event_sources is not None:
            return refreshed_event_sources
        return []


async def _resolve_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: EventCache,
    cached_event_sources: Sequence[dict[str, Any]],
) -> list[ResolvedVisibleMessage] | None:
    """Resolve cached thread history or invalidate the cache entry on corruption."""
    try:
        return await _resolve_thread_history_from_event_sources(
            client,
            thread_id=thread_id,
            event_sources=cached_event_sources,
        )
    except Exception as exc:
        logger.warning(
            "Cached thread payload could not be resolved; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        return None


async def _invalidate_thread_cache_entry(
    event_cache: EventCache,
    *,
    room_id: str,
    thread_id: str,
) -> None:
    """Best-effort invalidation for one broken cached thread entry."""
    try:
        await event_cache.invalidate_thread(room_id, thread_id)
    except Exception:
        logger.warning(
            "Failed to invalidate broken event cache entry",
            room_id=room_id,
            thread_id=thread_id,
        )


async def _fetch_thread_history_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> tuple[list[ResolvedVisibleMessage], list[dict[str, Any]]]:
    """Fetch thread history and raw event sources from the homeserver."""
    try:
        return await _fetch_thread_history_via_relations_with_events(client, room_id, thread_id)
    except _ThreadHistoryFastPathUnavailableError as exc:
        logger.info(
            "Falling back to room scan for thread history",
            room_id=room_id,
            thread_id=thread_id,
            reason=str(exc),
        )
        return await _fetch_thread_history_via_room_messages_with_events(client, room_id, thread_id)


async def _store_thread_history_cache(
    event_cache: EventCache,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
) -> None:
    """Best-effort replacement of one cached thread snapshot."""
    try:
        await event_cache.invalidate_thread(room_id, thread_id)
        await event_cache.store_thread_events(room_id, thread_id, list(event_sources))
    except Exception as exc:
        logger.warning(
            "Event cache write failed; continuing without cache",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )


def _bundled_replacement_event(
    event_source: dict[str, Any],
) -> nio.RoomMessageText | nio.RoomMessageNotice | None:
    """Return bundled replacement data when the homeserver included it inline."""
    for container in (
        event_source.get("unsigned"),
        event_source,
    ):
        if not isinstance(container, dict):
            continue
        relations = container.get("m.relations")
        if not isinstance(relations, dict):
            continue
        replacement = relations.get("m.replace")
        if not isinstance(replacement, dict):
            continue
        for candidate in (
            replacement,
            replacement.get("event"),
            replacement.get("latest_event"),
        ):
            if not isinstance(candidate, dict):
                continue
            if (parsed_event := _parse_room_message_event(candidate)) is not None:
                return parsed_event
    return None


async def _collect_related_events(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    rel_type: RelationshipType,
    event_type: str,
    direction: nio.MessageDirection = nio.MessageDirection.back,
    limit: int | None = None,
    max_events: int | None = None,
) -> list[nio.Event]:
    """Collect a relations iterator into a concrete list."""
    if max_events is not None and max_events <= 0:
        return []

    events: list[nio.Event] = []
    try:
        async for event in client.room_get_event_relations(
            room_id,
            event_id,
            rel_type=rel_type,
            event_type=event_type,
            direction=direction,
            limit=limit,
        ):
            events.append(event)
            if max_events is not None and len(events) >= max_events:
                return events
    except Exception as exc:
        msg = f"relations lookup failed for {event_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg) from exc
    return events


async def _fetch_latest_message_replacement(
    client: nio.AsyncClient,
    room_id: str,
    event: nio.RoomMessageText | nio.RoomMessageNotice,
) -> tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None] | None:
    """Return the latest replacement event for one message when available."""
    if (bundled_replacement := _bundled_replacement_event(event.source)) is not None:
        bundled_info = EventInfo.from_event(bundled_replacement.source)
        if bundled_info.original_event_id != event.event_id:
            msg = f"bundled replacement did not target {event.event_id}"
            raise _ThreadHistoryFastPathUnavailableError(msg)
        return bundled_replacement, bundled_info.thread_id_from_edit

    replacement_events = await _collect_related_events(
        client,
        room_id,
        event.event_id,
        rel_type=RelationshipType.replacement,
        event_type="m.room.message",
    )
    latest_replacement: nio.RoomMessageText | nio.RoomMessageNotice | None = None
    latest_replacement_thread_id: str | None = None
    for replacement_event in replacement_events:
        if not isinstance(replacement_event, (nio.RoomMessageText, nio.RoomMessageNotice)):
            continue
        replacement_info = EventInfo.from_event(replacement_event.source)
        if replacement_info.original_event_id != event.event_id:
            msg = f"replacement relation did not target {event.event_id}"
            raise _ThreadHistoryFastPathUnavailableError(msg)
        if latest_replacement is None or (replacement_event.server_timestamp, replacement_event.event_id) > (
            latest_replacement.server_timestamp,
            latest_replacement.event_id,
        ):
            latest_replacement = replacement_event
            latest_replacement_thread_id = replacement_info.thread_id_from_edit
    if latest_replacement is None:
        return None
    return latest_replacement, latest_replacement_thread_id


async def _collect_thread_relation_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> tuple[
    list[nio.RoomMessageText | nio.RoomMessageNotice],
    dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
]:
    """Collect direct thread children and inline edit candidates from relations."""
    relation_events = await _collect_related_events(
        client,
        room_id,
        thread_id,
        rel_type=RelationshipType.thread,
        event_type="m.room.message",
    )
    thread_events: list[nio.RoomMessageText | nio.RoomMessageNotice] = []
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}
    for event in relation_events:
        if not isinstance(event, (nio.RoomMessageText, nio.RoomMessageNotice)):
            continue
        event_info = EventInfo.from_event(event.source)
        if _record_latest_thread_edit(
            event,
            event_info=event_info,
            latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        ):
            continue
        if event_info.thread_id == thread_id:
            thread_events.append(event)
    return thread_events, latest_edits_by_original_event_id


async def _resolve_thread_history_message(
    event: nio.Event,
    client: nio.AsyncClient,
) -> ResolvedVisibleMessage:
    """Resolve one room-message event into the normalized thread-history shape."""
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
        message_data = await extract_and_resolve_message(event, client)
        return ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=EventInfo.from_event(event.source).thread_id,
            latest_event_id=event.event_id,
        )

    resolved_event_source = await resolve_event_source_content(
        event.source if isinstance(event.source, dict) else {},
        client,
    )
    content = resolved_event_source.get("content", {})
    normalized_content = content if isinstance(content, dict) else {}
    event_info = EventInfo.from_event(resolved_event_source)
    message = ResolvedVisibleMessage.synthetic(
        sender=event.sender,
        body=visible_body_from_event_source(resolved_event_source, _room_message_fallback_body(event)),
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
        event_id=event.event_id,
        content=normalized_content,
        thread_id=event_info.thread_id,
    )
    message.refresh_stream_status()
    return message


async def _record_relations_history_messages(
    client: nio.AsyncClient,
    *,
    root_message_event: nio.Event | None,
    thread_events: list[nio.Event],
) -> dict[str, ResolvedVisibleMessage]:
    """Resolve the root and direct thread children into canonical visible messages."""
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    for event in [root_message_event, *thread_events]:
        if event is None:
            continue
        messages_by_event_id[event.event_id] = await _resolve_thread_history_message(event, client)
    return messages_by_event_id


def _stream_status_from_content(content: dict[str, Any] | None) -> str | None:
    """Extract persisted stream status from message content when present."""
    if content is None:
        return None
    status = content.get(STREAM_STATUS_KEY)
    return status if isinstance(status, str) else None


def _record_latest_thread_edit(
    event: nio.RoomMessageText | nio.RoomMessageNotice,
    *,
    event_info: EventInfo,
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
) -> bool:
    """Track latest edit candidate, returning True if event is an edit."""
    if not (event_info.is_edit and event_info.original_event_id):
        return False

    original_event_id = event_info.original_event_id
    current_latest_edit_data = latest_edits_by_original_event_id.get(original_event_id)
    current_latest_edit = current_latest_edit_data[0] if current_latest_edit_data else None
    if current_latest_edit is None or (event.server_timestamp, event.event_id) > (
        current_latest_edit.server_timestamp,
        current_latest_edit.event_id,
    ):
        latest_edits_by_original_event_id[original_event_id] = (event, event_info.thread_id_from_edit)
    return True


async def _record_thread_message(
    event: nio.Event,
    *,
    event_info: EventInfo,
    client: nio.AsyncClient,
    thread_id: str,
    root_message_found: bool,
    messages_by_event_id: dict[str, ResolvedVisibleMessage],
) -> bool:
    """Record root/thread message into history and return updated root flag."""
    if event.event_id in messages_by_event_id:
        return root_message_found

    is_root_message = event.event_id == thread_id
    is_thread_message = event_info.is_thread and event_info.thread_id == thread_id

    if is_root_message and not root_message_found:
        messages_by_event_id[event.event_id] = await _resolve_thread_history_message(event, client)
        return True

    if is_thread_message:
        messages_by_event_id[event.event_id] = await _resolve_thread_history_message(event, client)

    return root_message_found


async def _apply_latest_edits_to_messages(
    client: nio.AsyncClient,
    *,
    messages_by_event_id: dict[str, ResolvedVisibleMessage],
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
    required_thread_id: str | None = None,
) -> None:
    """Apply latest edits to message records and synthesize missing originals when allowed."""
    for original_event_id, (edit_event, edit_thread_id) in latest_edits_by_original_event_id.items():
        existing_message = messages_by_event_id.get(original_event_id)

        # Ignore missing originals unrelated to this thread before resolving
        # potentially large edit payloads from sidecar storage.
        if existing_message is None and required_thread_id is not None and edit_thread_id != required_thread_id:
            continue

        edited_body, edited_content = await extract_edit_body(edit_event.source, client)
        if edited_body is None:
            continue

        if existing_message is not None:
            existing_message.apply_edit(
                body=edited_body,
                timestamp=edit_event.server_timestamp,
                latest_event_id=edit_event.event_id,
                thread_id=edit_thread_id,
                content=edited_content,
            )
            continue

        synthesized_message = ResolvedVisibleMessage(
            sender=edit_event.sender,
            body=edited_body,
            timestamp=edit_event.server_timestamp,
            event_id=original_event_id,
            content=edited_content if edited_content is not None else {},
            thread_id=edit_thread_id,
            latest_event_id=edit_event.event_id,
        )
        synthesized_message.refresh_stream_status()
        messages_by_event_id[original_event_id] = synthesized_message


async def resolve_latest_visible_messages(
    events: Sequence[nio.RoomMessageText | nio.RoomMessageNotice],
    client: nio.AsyncClient,
    *,
    sender: str | None = None,
) -> dict[str, ResolvedVisibleMessage]:
    """Resolve the latest visible message state by original event ID for a set of message events."""
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}

    for event in events:
        if sender is not None and event.sender != sender:
            continue

        event_info = EventInfo.from_event(event.source)
        if _record_latest_thread_edit(
            event,
            event_info=event_info,
            latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        ):
            continue

        if event.event_id in messages_by_event_id:
            continue

        message_data = await extract_and_resolve_message(event, client)
        messages_by_event_id[event.event_id] = ResolvedVisibleMessage.from_message_data(
            message_data,
            thread_id=event_info.thread_id,
            latest_event_id=event.event_id,
        )

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
    )
    return messages_by_event_id


async def fetch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: EventCache | None = None,
) -> list[ResolvedVisibleMessage]:
    """Fetch all messages in a thread."""
    active_event_cache = event_cache or get_event_cache(client)
    if active_event_cache is None:
        return await _fetch_thread_history_without_cache(client, room_id, thread_id)

    cached_history = await _load_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=active_event_cache,
    )
    if cached_history is not None:
        return cached_history

    thread_messages, event_sources = await _fetch_thread_history_with_events(client, room_id, thread_id)
    await _store_thread_history_cache(
        active_event_cache,
        room_id=room_id,
        thread_id=thread_id,
        event_sources=event_sources,
    )

    return thread_messages


async def _fetch_thread_history_via_relations_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> tuple[list[ResolvedVisibleMessage], list[dict[str, Any]]]:
    """Fetch thread history through relations plus explicit root lookup."""
    try:
        root_response = await client.room_get_event(room_id, thread_id)
    except Exception as exc:
        msg = f"root lookup failed for {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg) from exc
    if not isinstance(root_response, nio.RoomGetEventResponse):
        msg = f"failed to fetch thread root {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg)

    thread_events, latest_edits_by_original_event_id = await _collect_thread_relation_events(
        client,
        room_id,
        thread_id,
    )
    if not thread_events:
        msg = f"no direct thread children returned for {thread_id}"
        raise _ThreadHistoryFastPathUnavailableError(msg)

    root_message = root_response.event
    root_message_event = root_message if _is_room_message_event(root_message) else None
    messages_by_event_id = await _record_relations_history_messages(
        client,
        root_message_event=root_message_event,
        thread_events=thread_events,
    )

    for event in [root_message_event, *thread_events]:
        if event is None or not isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
            continue
        replacement = await _fetch_latest_message_replacement(client, room_id, event)
        if replacement is not None:
            latest_edits_by_original_event_id[event.event_id] = replacement

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        required_thread_id=thread_id,
    )
    messages = list(messages_by_event_id.values())
    _sort_thread_history_root_first(messages, thread_id=thread_id)
    event_sources: list[dict[str, Any]] = []
    if root_message_event is not None:
        event_sources.append(_event_source_for_cache(root_message_event))
    event_sources.extend(_event_source_for_cache(event) for event in thread_events)
    event_sources.extend(
        _event_source_for_cache(edit_event) for edit_event, _ in latest_edits_by_original_event_id.values()
    )
    return messages, event_sources


async def _fetch_thread_history_via_relations(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Fetch thread history through relations plus explicit root lookup."""
    messages, _event_sources = await _fetch_thread_history_via_relations_with_events(client, room_id, thread_id)
    return messages


async def _fetch_thread_history_via_room_messages_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> tuple[list[ResolvedVisibleMessage], list[dict[str, Any]]]:
    """Fetch all thread messages by scanning room history pages."""
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}
    relevant_message_sources: dict[str, dict[str, Any]] = {}
    from_token = None
    root_message_found = False

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": ["m.room.message"]},
            direction=nio.MessageDirection.back,
        )

        if not isinstance(response, nio.RoomMessagesResponse):
            logger.error("Failed to fetch thread history", room_id=room_id, error=str(response))
            break

        if not response.chunk:
            break

        for event in response.chunk:
            if not isinstance(event, nio.Event) or not _is_room_message_event(event):
                continue

            event_info = EventInfo.from_event(event.source)
            if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and _record_latest_thread_edit(
                event,
                event_info=event_info,
                latest_edits_by_original_event_id=latest_edits_by_original_event_id,
            ):
                continue
            if event_info.is_edit:
                continue

            if event.event_id == thread_id or (event_info.is_thread and event_info.thread_id == thread_id):
                relevant_message_sources[event.event_id] = _event_source_for_cache(event)

            root_message_found = await _record_thread_message(
                event,
                event_info=event_info,
                client=client,
                thread_id=thread_id,
                root_message_found=root_message_found,
                messages_by_event_id=messages_by_event_id,
            )

        if root_message_found or not response.end:
            break
        from_token = response.end

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        required_thread_id=thread_id,
    )
    messages = list(messages_by_event_id.values())
    _sort_thread_history_root_first(messages, thread_id=thread_id)
    relevant_event_ids = {message.event_id for message in messages}
    event_sources = list(relevant_message_sources.values())
    event_sources.extend(
        _event_source_for_cache(edit_event)
        for original_event_id, (edit_event, edit_thread_id) in latest_edits_by_original_event_id.items()
        if original_event_id in relevant_event_ids or edit_thread_id == thread_id
    )
    return messages, event_sources


async def _fetch_thread_history_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Fetch all thread messages by scanning room history pages."""
    messages, _event_sources = await _fetch_thread_history_via_room_messages_with_events(client, room_id, thread_id)
    return messages


async def _fetch_incremental_thread_events(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    cached_event_ids: Collection[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """Fetch only recent timeline events that may affect one cached thread."""
    from_token = None
    relevant_message_sources: list[dict[str, Any]] = []
    relevant_message_ids: set[str] = set()
    candidate_edit_sources: list[tuple[str | None, str | None, dict[str, Any]]] = []
    candidate_redactions: list[tuple[str, dict[str, Any]]] = []

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            msg = f"failed to fetch incremental thread history for {thread_id}"
            raise _ThreadHistoryFastPathUnavailableError(msg)
        if not response.chunk:
            break

        reached_cached_history = False
        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            if event.event_id in cached_event_ids:
                reached_cached_history = True
                break

            if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                event_info = EventInfo.from_event(event.source)
                event_source = _event_source_for_cache(event)
                if event_info.is_edit:
                    candidate_edit_sources.append(
                        (event_info.original_event_id, event_info.thread_id_from_edit, event_source),
                    )
                    continue
                if event.event_id == thread_id or (event_info.is_thread and event_info.thread_id == thread_id):
                    relevant_message_sources.append(event_source)
                    relevant_message_ids.add(event.event_id)
                    continue

            if isinstance(event, nio.RedactionEvent):
                candidate_redactions.append((event.redacts, _event_source_for_cache(event)))

        if reached_cached_history or not response.end:
            break
        from_token = response.end

    relevant_event_ids = set(cached_event_ids)
    relevant_event_ids.update(relevant_message_ids)

    relevant_edit_sources: list[dict[str, Any]] = []
    for original_event_id, edit_thread_id, event_source in candidate_edit_sources:
        touches_thread = edit_thread_id == thread_id
        touches_root = original_event_id == thread_id
        touches_known_event = original_event_id is not None and original_event_id in relevant_event_ids
        if touches_thread or touches_root or touches_known_event:
            relevant_edit_sources.append(event_source)
            event_id = _event_id_from_source(event_source)
            if event_id is not None:
                relevant_event_ids.add(event_id)

    relevant_redactions = [
        (redacted_event_id, redaction_source)
        for redacted_event_id, redaction_source in candidate_redactions
        if redacted_event_id in relevant_event_ids
    ]
    redacted_event_ids = {redacted_event_id for redacted_event_id, _ in relevant_redactions}
    event_sources = [
        event_source
        for event_source in [*relevant_message_sources, *relevant_edit_sources]
        if (event_id := _event_id_from_source(event_source)) is None or event_id not in redacted_event_ids
    ]
    return event_sources, relevant_redactions


async def fetch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> ThreadHistoryResult:
    """Fetch lightweight thread context for dispatch decisions."""
    try:
        return _thread_history_result(
            await _fetch_thread_context_via_relations(client, room_id, thread_id),
            is_full_history=False,
        )
    except _ThreadHistoryFastPathUnavailableError:
        return _thread_history_result(
            await _fetch_thread_history_via_room_messages(client, room_id, thread_id),
            is_full_history=True,
        )


async def get_room_threads_page(
    client: nio.AsyncClient,
    room_id: str,
    *,
    limit: int,
    page_token: str | None = None,
) -> tuple[list[nio.Event], str | None]:
    """Fetch a single page of thread roots for a room."""
    if not client.access_token:
        raise RoomThreadsPageError(
            response="Matrix client access token is required for room thread pagination.",
        )

    method, path = nio.Api.room_get_threads(
        client.access_token,
        room_id,
        paginate_from=page_token,
        limit=limit,
    )
    try:
        response = await client._send(  # matrix-nio only exposes single-page /threads via the private transport helper
            RoomThreadsResponse,
            method,
            path,
            response_data=(room_id,),
        )
    except (ClientError, TimeoutError) as exc:
        raise _room_threads_page_error_from_exception(exc) from exc
    if not isinstance(response, RoomThreadsResponse):
        raise _room_threads_page_error_from_response(response)

    return response.thread_roots, response.next_batch


async def _latest_thread_event_id(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> str:
    """Get the latest visible event ID in a thread for MSC3440 fallback compliance."""
    try:
        thread_messages = await fetch_thread_history(client, room_id, thread_id)
    except Exception:
        return thread_id
    if thread_messages:
        last_event_id = thread_messages[-1].visible_event_id
        if last_event_id:
            return last_event_id

    try:
        relation_events = await _collect_related_events(
            client,
            room_id,
            thread_id,
            rel_type=RelationshipType.thread,
            event_type="m.room.message",
        )
    except _ThreadHistoryFastPathUnavailableError:
        return thread_id

    latest_thread_edit: nio.RoomMessageText | nio.RoomMessageNotice | None = None
    for event in relation_events:
        if not isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
            continue
        event_info = EventInfo.from_event(event.source)
        if not event_info.is_edit or event_info.thread_id_from_edit != thread_id:
            continue
        candidate_key = (
            event.server_timestamp if isinstance(event.server_timestamp, int) else 0,
            event.event_id,
        )
        latest_key = (
            latest_thread_edit.server_timestamp
            if latest_thread_edit and isinstance(latest_thread_edit.server_timestamp, int)
            else 0,
            latest_thread_edit.event_id if latest_thread_edit is not None else "",
        )
        if latest_thread_edit is None or candidate_key > latest_key:
            latest_thread_edit = event
    if latest_thread_edit is not None:
        return latest_thread_edit.event_id
    return thread_id


async def get_latest_thread_event_id_if_needed(
    client: nio.AsyncClient | None,
    room_id: str,
    thread_id: str | None,
    reply_to_event_id: str | None = None,
    existing_event_id: str | None = None,
) -> str | None:
    """Get the latest thread event ID only when needed for MSC3440 compliance.

    This helper encapsulates the common pattern of conditionally fetching
    the latest thread event ID based on various conditions.

    Args:
        client: Matrix client (can be None)
        room_id: Room ID
        thread_id: Thread root event ID (can be None)
        reply_to_event_id: Event ID being replied to (if any)
        existing_event_id: Existing event ID being edited (if any)

    Returns:
        The latest event ID in the thread if needed, None otherwise

    """
    # Only fetch latest thread event when:
    # 1. We have a thread_id
    # 2. We have a client
    # 3. We're not editing an existing message
    # 4. We're not making a genuine reply
    if thread_id and client and not existing_event_id and not reply_to_event_id:
        return await _latest_thread_event_id(client, room_id, thread_id)
    return None


async def build_threaded_edit_content(
    client: nio.AsyncClient,
    *,
    room_id: str,
    new_text: str,
    thread_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    sender_domain: str,
    tool_trace: list[Any] | None = None,
    extra_content: dict[str, Any] | None = None,
    latest_thread_event_id: str | None = None,
) -> dict[str, Any]:
    """Build edit content that preserves thread fallback semantics when needed."""
    latest_visible_thread_event_id = latest_thread_event_id
    if thread_id is not None and latest_visible_thread_event_id is None:
        latest_visible_thread_event_id = await _latest_thread_event_id(client, room_id, thread_id)

    return format_message_with_mentions(
        config,
        runtime_paths,
        new_text,
        sender_domain=sender_domain,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_visible_thread_event_id,
        tool_trace=tool_trace,
        extra_content=extra_content,
    )


async def edit_message(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    extra_content: dict[str, Any] | None = None,
) -> str | None:
    """Edit an existing Matrix message.

    Automatically handles large messages that exceed the Matrix event size limit
    by uploading the full content as MXC and sending a maximum-size preview.

    Args:
        client: The Matrix client
        room_id: The room ID where the message is
        event_id: The event ID of the message to edit
        new_content: The new content dictionary (from format_message_with_mentions)
        new_text: The new text (plain text version)
        extra_content: Optional extra keys to merge into both edit payload layers

    Returns:
        The event ID of the edit message, or None if editing failed

    """
    replacement_content = dict(new_content)
    edit_content = {
        "msgtype": "m.text",
        "body": f"* {new_text}",
        "format": "org.matrix.custom.html",
        "formatted_body": new_content.get("formatted_body", new_text),
        "m.new_content": replacement_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
    }
    if extra_content:
        replacement_content.update(extra_content)
        edit_content.update(extra_content)

    # send_message will handle large messages, including the lower threshold for edits
    return await send_message(client, room_id, edit_content)
