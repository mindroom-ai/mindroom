"""Matrix client operations and utilities."""

import json
import ssl as ssl_module
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import nio

from mindroom.config.matrix import RoomDirectoryVisibility, RoomJoinRule
from mindroom.constants import ENCRYPTION_KEYS_DIR, MATRIX_SSL_VERIFY
from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body

logger = get_logger(__name__)


def _maybe_ssl_context(homeserver: str) -> ssl_module.SSLContext | None:
    if homeserver.startswith("https://"):
        if not MATRIX_SSL_VERIFY:
            # Create context that disables verification for dev/self-signed certs
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            # Use default context with proper verification
            ssl_context = ssl_module.create_default_context()
        return ssl_context
    return None


def create_matrix_client(
    homeserver: str,
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

    Returns:
        nio.AsyncClient: Configured Matrix client instance

    """
    ssl_context = _maybe_ssl_context(homeserver)

    # Default store path for encryption support
    if store_path is None and user_id:
        safe_user_id = user_id.replace(":", "_").replace("@", "")
        store_path = str(ENCRYPTION_KEYS_DIR / safe_user_id)
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
    user_id: str | None = None,
    access_token: str | None = None,
) -> AsyncGenerator[nio.AsyncClient, None]:
    """Context manager for Matrix client that ensures proper cleanup.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: Optional user ID for authenticated client
        access_token: Optional access token for authenticated client

    Yields:
        nio.AsyncClient: The Matrix client instance

    Example:
        async with matrix_client("http://localhost:8008") as client:
            response = await client.login(password="secret")

    """
    client = create_matrix_client(homeserver, user_id, access_token)

    try:
        yield client
    finally:
        await client.close()


async def login(homeserver: str, user_id: str, password: str) -> nio.AsyncClient:
    """Login to Matrix and return authenticated client.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: The full Matrix user ID (e.g., @user:localhost)
        password: The user's password

    Returns:
        Authenticated AsyncClient instance

    Raises:
        ValueError: If login fails

    """
    client = create_matrix_client(homeserver, user_id)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        logger.info(f"Successfully logged in: {user_id}")
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise ValueError(msg)


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

    if power_users:
        power_level_content: dict[str, Any] = {
            "users": dict.fromkeys(power_users, 50),
            "state_default": 50,  # Set default required power for state events
        }
        # Ensure the creator is an admin
        if client.user_id:
            power_level_content["users"][client.user_id] = 100
        room_config["initial_state"] = [{"type": "m.room.power_levels", "content": power_level_content}]

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


def _describe_matrix_response_error(response: object) -> str:
    """Convert a Matrix response object into a concise error string."""
    status_code = getattr(response, "status_code", None)
    message = getattr(response, "message", None)
    if status_code and message:
        return f"{status_code}: {message}"
    if status_code:
        return str(status_code)
    if message:
        return str(message)
    return str(response)


async def get_room_join_rule(client: nio.AsyncClient, room_id: str) -> str | None:
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


async def set_room_join_rule(
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
    current_join_rule = await get_room_join_rule(client, room_id)
    if current_join_rule == target_join_rule:
        logger.debug("Room join rule already configured", room_id=room_id, join_rule=target_join_rule)
        return True
    return await set_room_join_rule(client, room_id, target_join_rule)


async def get_room_directory_visibility(client: nio.AsyncClient, room_id: str) -> str | None:
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


async def set_room_directory_visibility(
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
    current_visibility = await get_room_directory_visibility(client, room_id)
    if current_visibility == target_visibility:
        logger.debug("Room directory visibility already configured", room_id=room_id, visibility=target_visibility)
        return True
    return await set_room_directory_visibility(client, room_id, target_visibility)


async def create_dm_room(
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
    # Try to get the room name directly
    response = await client.room_get_state_event(room_id, "m.room.name")
    if isinstance(response, nio.RoomGetStateEventResponse) and response.content.get("name"):
        return str(response.content["name"])

    # Get room state for fallback naming
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return "Unnamed Room"

    # Check for room name in state events
    for event in response.events:
        if event.get("type") == "m.room.name" and event.get("content", {}).get("name"):
            return str(event["content"]["name"])

    # Build member list for DM/group room names
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


def _history_message_sort_key(message: dict[str, Any]) -> tuple[int, str]:
    """Sort thread history messages by timestamp and event ID."""
    return (message["timestamp"], message["event_id"])


def _record_latest_thread_edit(
    event: nio.RoomMessageText,
    *,
    event_info: EventInfo,
    thread_id: str,
    latest_edits_by_original_event_id: dict[str, nio.RoomMessageText],
) -> bool:
    """Track latest relevant edit for a thread, returning True if event is an edit."""
    if not (event_info.is_edit and event_info.thread_id_from_edit == thread_id and event_info.original_event_id):
        return False

    original_event_id = event_info.original_event_id
    current_latest_edit = latest_edits_by_original_event_id.get(original_event_id)
    if current_latest_edit is None or (event.server_timestamp, event.event_id) > (
        current_latest_edit.server_timestamp,
        current_latest_edit.event_id,
    ):
        latest_edits_by_original_event_id[original_event_id] = event
    return True


async def _record_thread_message(
    event: nio.RoomMessageText,
    *,
    event_info: EventInfo,
    client: nio.AsyncClient,
    thread_id: str,
    root_message_found: bool,
    messages: list[dict[str, Any]],
    messages_by_event_id: dict[str, dict[str, Any]],
) -> bool:
    """Record root/thread message into history and return updated root flag."""
    if event.event_id in messages_by_event_id:
        return root_message_found

    is_root_message = event.event_id == thread_id
    is_thread_message = event_info.is_thread and event_info.thread_id == thread_id

    if is_root_message and not root_message_found:
        message_data = await extract_and_resolve_message(event, client)
        messages.append(message_data)
        messages_by_event_id[event.event_id] = message_data
        return True

    if is_thread_message:
        message_data = await extract_and_resolve_message(event, client)
        messages.append(message_data)
        messages_by_event_id[event.event_id] = message_data

    return root_message_found


async def _apply_thread_edits_to_history(
    client: nio.AsyncClient,
    *,
    messages: list[dict[str, Any]],
    messages_by_event_id: dict[str, dict[str, Any]],
    latest_edits_by_original_event_id: dict[str, nio.RoomMessageText],
) -> None:
    """Apply latest edits to history entries and synthesize missing originals."""
    for original_event_id, edit_event in latest_edits_by_original_event_id.items():
        edited_body, edited_content = await extract_edit_body(edit_event.source, client)
        if edited_body is None:
            continue

        existing_message = messages_by_event_id.get(original_event_id)
        if existing_message is not None:
            existing_message["body"] = edited_body
            if edited_content is not None:
                existing_message["content"] = edited_content
            continue

        synthesized_message = {
            "sender": edit_event.sender,
            "body": edited_body,
            "timestamp": edit_event.server_timestamp,
            "event_id": original_event_id,
            "content": edited_content if edited_content is not None else {},
        }
        messages.append(synthesized_message)
        messages_by_event_id[original_event_id] = synthesized_message


async def fetch_thread_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> list[dict[str, Any]]:
    """Fetch all messages in a thread.

    Args:
        client: The Matrix client instance
        room_id: The room ID to fetch messages from
        thread_id: The thread root event ID

    Returns:
        List of messages in chronological order, each containing sender, body, timestamp, and event_id

    """
    messages: list[dict[str, Any]] = []
    messages_by_event_id: dict[str, dict[str, Any]] = {}
    latest_edits_by_original_event_id: dict[str, nio.RoomMessageText] = {}
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

        # Break if no new messages found
        if not response.chunk:
            break

        for event in response.chunk:
            if not isinstance(event, nio.RoomMessageText):
                continue

            event_info = EventInfo.from_event(event.source)
            if _record_latest_thread_edit(
                event,
                event_info=event_info,
                thread_id=thread_id,
                latest_edits_by_original_event_id=latest_edits_by_original_event_id,
            ):
                continue

            root_message_found = await _record_thread_message(
                event,
                event_info=event_info,
                client=client,
                thread_id=thread_id,
                root_message_found=root_message_found,
                messages=messages,
                messages_by_event_id=messages_by_event_id,
            )

        # Once the thread root is seen, all older pages are outside this thread.
        if root_message_found or not response.end:
            break
        from_token = response.end

    await _apply_thread_edits_to_history(
        client,
        messages=messages,
        messages_by_event_id=messages_by_event_id,
        latest_edits_by_original_event_id=latest_edits_by_original_event_id,
    )
    messages.sort(key=_history_message_sort_key)
    return messages


async def _latest_thread_event_id(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> str:
    """Get the latest event ID in a thread for MSC3440 fallback compliance.

    This function fetches the thread history and returns the latest event ID.
    If the thread has no messages yet, returns the thread_id itself as fallback.

    Args:
        client: Matrix client
        room_id: Room ID
        thread_id: Thread root event ID

    Returns:
        The latest event ID in the thread, or thread_id if thread is empty

    """
    thread_msgs = await fetch_thread_history(client, room_id, thread_id)
    if thread_msgs:
        last_event_id = thread_msgs[-1].get("event_id")
        return str(last_event_id) if last_event_id else thread_id
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


async def edit_message(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
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

    Returns:
        The event ID of the edit message, or None if editing failed

    """
    edit_content = {
        "msgtype": "m.text",
        "body": f"* {new_text}",
        "format": "org.matrix.custom.html",
        "formatted_body": new_content.get("formatted_body", new_text),
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
    }

    # send_message will handle large messages, including the lower threshold for edits
    return await send_message(client, room_id, edit_content)
