"""Matrix client operations and utilities."""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import ssl as ssl_module
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio
from aiohttp import ClientError
from nio import crypto
from nio.responses import RoomThreadsResponse

from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.cache.event_cache import (
    ConversationEventCache,
    normalize_nio_event_for_cache,
)
from mindroom.matrix.cache.thread_cache_helpers import thread_cache_state_is_usable
from mindroom.matrix.cache.thread_history_result import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ThreadHistoryResult,
    thread_history_result,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import (
    extract_and_resolve_message,
    extract_edit_body,
    resolve_event_source_content,
    visible_body_from_event_source,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.matrix import RoomDirectoryVisibility, RoomJoinRule
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

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
    ) -> ResolvedVisibleMessage:
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
    ) -> ResolvedVisibleMessage:
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


@dataclass(slots=True)
class _ThreadHistoryFetchResult:
    """Resolved thread history plus the raw sources and timing diagnostics used to build it."""

    history: list[ResolvedVisibleMessage]
    event_sources: list[dict[str, Any]]
    resolution_ms: float
    sidecar_hydration_ms: float


@dataclass(frozen=True, slots=True)
class DeliveredMatrixEvent:
    """One successfully delivered Matrix event plus the exact sent content payload."""

    event_id: str
    content_sent: dict[str, Any]


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
    diagnostics: Mapping[str, str | int | float | bool] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    return thread_history_result(history, is_full_history=is_full_history, diagnostics=diagnostics)


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
        logger.info("matrix_room_created", room_id=str(response.room_id), name=name)
        room_id = str(response.room_id)

        # Invite power users to the room
        if power_users:
            for user_id in power_users:
                # Skip inviting ourselves
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
        logger.info("matrix_room_joined", room_id=room_id)
        return True
    logger.warning("matrix_room_join_failed", room_id=room_id, error=str(response))
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
    logger.warning("matrix_room_members_fetch_failed", room_id=room_id)
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
    logger.error("matrix_joined_rooms_fetch_failed", error=str(response))
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
        logger.info("matrix_room_left", room_id=room_id)
        return True
    logger.error("matrix_room_leave_failed", room_id=room_id, error=str(response))
    return False


def _can_send_to_encrypted_room(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool:
    """Return whether one outbound room operation can proceed with current nio E2EE support."""
    room = cached_room(client, room_id)
    if room is None or not room.encrypted or crypto.ENCRYPTION_ENABLED:
        return True
    logger.error(
        "matrix_e2ee_support_required",
        room_id=room_id,
        operation=operation,
        hint="Install `mindroom[matrix_e2ee]` or `matrix-nio[e2e]` to use encrypted Matrix rooms.",
    )
    return False

async def send_message_result(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
) -> DeliveredMatrixEvent | None:
    """Send a message to a Matrix room and return the exact delivered payload.

    Automatically handles large messages that exceed the Matrix event size limit
    by uploading the full content as MXC and sending a maximum-size preview.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to send the message to
        content: The message content dictionary

    Returns:
        The delivered event id plus the exact sent content, or None if sending failed

    """
    if not _can_send_to_encrypted_room(client, room_id, operation="send_message"):
        return None

    content_sent = await prepare_large_message(client, room_id, content)

    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content_sent,
    )
    if isinstance(response, nio.RoomSendResponse):
        logger.debug("matrix_message_sent", room_id=room_id, event_id=str(response.event_id))
        return DeliveredMatrixEvent(event_id=str(response.event_id), content_sent=content_sent)
    logger.error("matrix_message_send_failed", room_id=room_id, error=str(response))
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
    rooms = client.rooms
    room = rooms.get(room_id) if isinstance(rooms, dict) else None
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
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Upload a file and send it with the appropriate Matrix message type."""
    resolved_path = Path(file_path).expanduser().resolve()
    if not resolved_path.is_file():
        logger.error("Cannot send non-file attachment", path=str(resolved_path))
        return None
    if not _can_send_to_encrypted_room(client, room_id, operation="send_file_message"):
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
        if latest_thread_event_id is None:
            msg = "latest_thread_event_id is required for thread fallback"
            raise ValueError(msg)
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": latest_thread_event_id},
        }

    delivered = await send_message_result(client, room_id, content)
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


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


def _parse_room_message_event(event_source: dict[str, Any]) -> nio.Event | None:
    """Parse one event dict into a room-message event when possible."""
    try:
        parsed_event = nio.Event.parse_event(event_source)
    except Exception:
        return None
    return parsed_event if _is_room_message_event(parsed_event) else None


def _parse_visible_text_message_event(
    event_source: dict[str, Any],
) -> nio.RoomMessageText | nio.RoomMessageNotice | None:
    """Parse one event dict into a visible text or notice message when possible."""
    parsed_event = _parse_room_message_event(event_source)
    return parsed_event if isinstance(parsed_event, (nio.RoomMessageText, nio.RoomMessageNotice)) else None


def _event_source_for_cache(event: nio.Event) -> dict[str, Any]:
    """Normalize one nio event source for persistent cache storage."""
    return normalize_nio_event_for_cache(event)


def _event_id_from_source(event_source: Mapping[str, Any]) -> str | None:
    """Return one Matrix event ID from a raw event source when present."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def _bundled_replacement_source(event_source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one bundled replacement event source when Matrix already included it."""
    unsigned = event_source.get("unsigned")
    if not isinstance(unsigned, Mapping):
        return None
    relations = unsigned.get("m.relations")
    if not isinstance(relations, Mapping):
        return None
    replacement = relations.get("m.replace")
    if not isinstance(replacement, Mapping):
        return None
    candidates: tuple[object, ...] = (
        replacement.get("event"),
        replacement.get("latest_event"),
    )
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        normalized_candidate = {key: value for key, value in candidate.items() if isinstance(key, str)}
        if _parse_visible_text_message_event(normalized_candidate) is not None:
            return normalized_candidate
    replacement_candidate = {key: value for key, value in replacement.items() if isinstance(key, str)}
    if {
        "event_id",
        "sender",
        "type",
        "origin_server_ts",
    }.issubset(replacement_candidate) and _parse_visible_text_message_event(replacement_candidate) is not None:
        return replacement_candidate
    return None


async def _resolve_thread_history_from_event_sources_timed(
    client: nio.AsyncClient,
    *,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
    hydrate_sidecars: bool = True,
) -> tuple[list[ResolvedVisibleMessage], float]:
    """Resolve visible thread history and return approximate sidecar hydration time."""
    parsed_events = [
        parsed_event
        for event_source in event_sources
        if (parsed_event := _parse_room_message_event(event_source)) is not None
    ]
    messages_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]] = {}
    sidecar_hydration_started = time.perf_counter()
    for event in parsed_events:
        event_info = EventInfo.from_event(event.source)
        bundled_replacement_source = _bundled_replacement_source(event.source)
        if bundled_replacement_source is not None:
            bundled_replacement = nio.Event.parse_event(bundled_replacement_source)
            if isinstance(bundled_replacement, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES):
                _record_latest_thread_edit(
                    bundled_replacement,
                    event_info=EventInfo.from_event(bundled_replacement.source),
                    latest_edits_by_original_event_id=latest_edits_by_original_event_id,
                )
        if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and _record_latest_thread_edit(
            event,
            event_info=event_info,
            latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        ):
            continue
        if event_info.is_edit or event.event_id in messages_by_event_id:
            continue
        messages_by_event_id[event.event_id] = (
            await _resolve_thread_history_message(event, client) if hydrate_sidecars else _snapshot_message_dict(event)
        )

    await _apply_latest_edits_to_messages(
        client,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
        required_thread_id=thread_id,
    )
    messages = list(messages_by_event_id.values())
    _sort_thread_history_root_first(messages, thread_id=thread_id)
    return messages, round((time.perf_counter() - sidecar_hydration_started) * 1000, 1)


async def _load_stale_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    hydrate_sidecars: bool = True,
    fetch_error: Exception,
) -> ThreadHistoryResult | None:
    """Return stale cached thread history when a refetch fails but durable rows still exist."""
    cache_read_started = time.perf_counter()
    try:
        cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    except Exception as exc:
        logger.warning(
            "Failed to read stale thread cache after refetch failure",
            room_id=room_id,
            thread_id=thread_id,
            fetch_error=str(fetch_error),
            cache_error=str(exc),
        )
        return None
    if cached_event_sources is None:
        return None

    resolution_started = time.perf_counter()
    resolved_history, sidecar_hydration_ms = await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
        hydrate_sidecars=hydrate_sidecars,
    )
    if resolved_history is None:
        return None

    logger.warning(
        "Thread refetch failed; returning stale cached history",
        room_id=room_id,
        thread_id=thread_id,
        error=str(fetch_error),
    )
    return _thread_history_result(
        resolved_history,
        is_full_history=hydrate_sidecars,
        diagnostics={
            "cache_read_ms": round((time.perf_counter() - cache_read_started) * 1000, 1),
            "resolution_ms": round((time.perf_counter() - resolution_started) * 1000, 1),
            "sidecar_hydration_ms": sidecar_hydration_ms,
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
            THREAD_HISTORY_ERROR_DIAGNOSTIC: str(fetch_error),
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        },
    )


async def _resolve_cached_thread_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    cached_event_sources: Sequence[dict[str, Any]],
    hydrate_sidecars: bool = True,
) -> tuple[list[ResolvedVisibleMessage] | None, float]:
    """Resolve cached thread history or invalidate the cache entry on corruption."""
    try:
        return await _resolve_thread_history_from_event_sources_timed(
            client,
            thread_id=thread_id,
            event_sources=cached_event_sources,
            hydrate_sidecars=hydrate_sidecars,
        )
    except Exception as exc:
        logger.warning(
            "Cached thread payload could not be resolved; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        await _invalidate_thread_cache_entry(event_cache, room_id=room_id, thread_id=thread_id)
        return None, 0.0


async def _load_cached_thread_history_if_usable(
    client: nio.AsyncClient,
    *,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    hydrate_sidecars: bool,
    runtime_started_at: float | None,
) -> ThreadHistoryResult | None:
    """Return a fresh durable thread snapshot when the current runtime may safely trust it."""
    cache_state = await event_cache.get_thread_cache_state(room_id, thread_id)
    if not thread_cache_state_is_usable(
        cache_state,
        runtime_started_at=runtime_started_at,
    ):
        return None

    cache_read_started = time.perf_counter()
    cached_event_sources = await event_cache.get_thread_events(room_id, thread_id)
    if cached_event_sources is None:
        return None

    resolution_started = time.perf_counter()
    resolved_history, sidecar_hydration_ms = await _resolve_cached_thread_history(
        client,
        room_id=room_id,
        thread_id=thread_id,
        event_cache=event_cache,
        cached_event_sources=cached_event_sources,
        hydrate_sidecars=hydrate_sidecars,
    )
    if resolved_history is None:
        return None

    return _thread_history_result(
        resolved_history,
        is_full_history=hydrate_sidecars,
        diagnostics={
            "cache_read_ms": round((time.perf_counter() - cache_read_started) * 1000, 1),
            "resolution_ms": round((time.perf_counter() - resolution_started) * 1000, 1),
            "sidecar_hydration_ms": sidecar_hydration_ms,
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE,
        },
    )


async def _invalidate_thread_cache_entry(
    event_cache: ConversationEventCache,
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
    *,
    hydrate_sidecars: bool,
) -> _ThreadHistoryFetchResult:
    """Fetch thread history and raw event sources from the homeserver."""
    return await _fetch_thread_history_via_room_messages_with_events(
        client,
        room_id,
        thread_id,
        hydrate_sidecars=hydrate_sidecars,
    )


async def refresh_thread_history_from_source(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    hydrate_sidecars: bool = True,
    allow_stale_fallback: bool = True,
) -> ThreadHistoryResult:
    """Fetch fresh thread history from Matrix and repopulate the advisory cache."""
    try:
        fetch_result = await _fetch_thread_history_with_events(
            client,
            room_id,
            thread_id,
            hydrate_sidecars=hydrate_sidecars,
        )
    except Exception as exc:
        if allow_stale_fallback:
            stale_history = await _load_stale_cached_thread_history(
                client,
                room_id=room_id,
                thread_id=thread_id,
                event_cache=event_cache,
                hydrate_sidecars=hydrate_sidecars,
                fetch_error=exc,
            )
            if stale_history is not None:
                return stale_history
        raise
    if _thread_history_fetch_is_cacheable(fetch_result.event_sources, thread_id=thread_id):
        await _store_thread_history_cache(
            event_cache,
            room_id=room_id,
            thread_id=thread_id,
            event_sources=fetch_result.event_sources,
        )
    return _thread_history_result(
        fetch_result.history,
        is_full_history=hydrate_sidecars,
        diagnostics={
            "cache_read_ms": 0.0,
            "resolution_ms": fetch_result.resolution_ms,
            "sidecar_hydration_ms": fetch_result.sidecar_hydration_ms,
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
        },
    )


async def _store_thread_history_cache(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    event_sources: Sequence[dict[str, Any]],
) -> bool:
    """Best-effort replacement of one cached thread snapshot."""
    try:
        await event_cache.replace_thread(
            room_id,
            thread_id,
            list(event_sources),
        )
    except Exception as exc:
        logger.warning(
            "Event cache write failed; continuing without cache",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return False
    return True


def _thread_history_fetch_is_cacheable(
    event_sources: Sequence[dict[str, Any]],
    *,
    thread_id: str,
) -> bool:
    """Return whether one homeserver fetch contains the root event and is safe to cache."""
    return any(_event_id_from_source(event_source) == thread_id for event_source in event_sources)


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
    event_cache: ConversationEventCache,
    *,
    runtime_started_at: float | None = None,
) -> ThreadHistoryResult:
    """Fetch all messages in a thread."""
    try:
        cached_history = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=True,
            runtime_started_at=runtime_started_at,
        )
    except Exception as exc:
        logger.warning(
            "Durable thread cache read failed; refetching from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        allow_stale_fallback=True,
    )


async def fetch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
    *,
    runtime_started_at: float | None = None,
) -> ThreadHistoryResult:
    """Fetch lightweight thread context without hydrating sidecars when a fresh cache hit is unavailable."""
    try:
        cached_history = await _load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=event_cache,
            hydrate_sidecars=False,
            runtime_started_at=runtime_started_at,
        )
    except Exception as exc:
        logger.warning(
            "Durable thread cache read failed; refetching snapshot from homeserver",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
    else:
        if cached_history is not None:
            return cached_history
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=False,
        allow_stale_fallback=True,
    )


async def fetch_dispatch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
) -> ThreadHistoryResult:
    """Fetch authoritative full thread history for dispatch without durable-cache reuse or stale fallback."""
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=True,
        allow_stale_fallback=False,
    )


async def fetch_dispatch_thread_snapshot(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    event_cache: ConversationEventCache,
) -> ThreadHistoryResult:
    """Fetch authoritative lightweight thread context for dispatch without durable-cache reuse or stale fallback."""
    return await refresh_thread_history_from_source(
        client,
        room_id,
        thread_id,
        event_cache,
        hydrate_sidecars=False,
        allow_stale_fallback=False,
    )


async def _fetch_thread_history_via_room_messages_with_events(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    hydrate_sidecars: bool,
) -> _ThreadHistoryFetchResult:
    """Fetch all thread messages by scanning room history pages."""
    event_sources, _root_message_found = await _fetch_thread_event_sources_via_room_messages(client, room_id, thread_id)
    resolution_started = time.perf_counter()
    history, sidecar_hydration_ms = await _resolve_thread_history_from_event_sources_timed(
        client,
        thread_id=thread_id,
        event_sources=event_sources,
        hydrate_sidecars=hydrate_sidecars,
    )
    return _ThreadHistoryFetchResult(
        history=history,
        event_sources=event_sources,
        resolution_ms=round((time.perf_counter() - resolution_started) * 1000, 1),
        sidecar_hydration_ms=sidecar_hydration_ms,
    )


def _record_scanned_thread_event_source(
    event: nio.Event,
    *,
    thread_id: str,
    latest_edits_by_original_event_id: dict[str, tuple[nio.RoomMessageText | nio.RoomMessageNotice, str | None]],
    relevant_message_sources: dict[str, dict[str, Any]],
) -> bool:
    """Record one scanned event source and return whether the thread root was found."""
    if not _is_room_message_event(event):
        return False

    event_info = EventInfo.from_event(event.source)
    if isinstance(event, _VISIBLE_ROOM_MESSAGE_EVENT_TYPES) and _record_latest_thread_edit(
        event,
        event_info=event_info,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
    ):
        return False
    if event_info.is_edit:
        return False

    if event.event_id == thread_id or (event_info.is_thread and event_info.thread_id == thread_id):
        relevant_message_sources[event.event_id] = _event_source_for_cache(event)
    return event.event_id == thread_id


async def _fetch_thread_event_sources_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch thread event sources by scanning room history pages."""
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
            msg = f"room scan failed for {thread_id}: {response}"
            logger.error("Failed to fetch thread history", room_id=room_id, thread_id=thread_id, error=str(response))
            raise RuntimeError(msg)  # noqa: TRY004

        if not response.chunk:
            break

        for event in response.chunk:
            if not isinstance(event, nio.Event):
                continue
            if _record_scanned_thread_event_source(
                event,
                thread_id=thread_id,
                latest_edits_by_original_event_id=latest_edits_by_original_event_id,
                relevant_message_sources=relevant_message_sources,
            ):
                root_message_found = True

        if root_message_found or not response.end:
            break
        from_token = response.end

    if not root_message_found:
        msg = f"thread root {thread_id} not found during room scan"
        logger.warning(
            "Thread room scan ended without finding root",
            room_id=room_id,
            thread_id=thread_id,
            scanned_event_count=len(relevant_message_sources),
        )
        raise RuntimeError(msg)

    relevant_event_ids = set(relevant_message_sources)
    event_sources = list(relevant_message_sources.values())
    event_sources.extend(
        _event_source_for_cache(edit_event)
        for original_event_id, (edit_event, edit_thread_id) in latest_edits_by_original_event_id.items()
        if original_event_id in relevant_event_ids or edit_thread_id == thread_id
    )
    return event_sources, root_message_found


async def _fetch_thread_history_via_room_messages(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Fetch all thread messages by scanning room history pages."""
    return (
        await _fetch_thread_history_via_room_messages_with_events(
            client,
            room_id,
            thread_id,
            hydrate_sidecars=True,
        )
    ).history


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


def build_threaded_edit_content(
    *,
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
    if thread_id is not None and latest_thread_event_id is None:
        msg = "latest_thread_event_id is required for thread fallback"
        raise ValueError(msg)

    return format_message_with_mentions(
        config,
        runtime_paths,
        new_text,
        sender_domain=sender_domain,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        tool_trace=tool_trace,
        extra_content=extra_content,
    )


def build_edit_event_content(
    *,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap replacement content in one Matrix m.replace edit envelope."""
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
    return edit_content


async def edit_message_result(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    extra_content: dict[str, Any] | None = None,
) -> DeliveredMatrixEvent | None:
    """Edit an existing Matrix message and return the exact delivered payload.

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
        The delivered edit event plus exact sent content, or None if editing failed

    """
    edit_content = build_edit_event_content(
        event_id=event_id,
        new_content=new_content,
        new_text=new_text,
        extra_content=extra_content,
    )

    return await send_message_result(client, room_id, edit_content)
