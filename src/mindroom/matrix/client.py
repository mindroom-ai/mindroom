"""Matrix client operations and utilities."""

import io
import os
import ssl as ssl_module
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import markdown
import nio

from mindroom.logging_config import get_logger

from .identity import MatrixID, extract_server_name_from_homeserver

logger = get_logger(__name__)


def extract_thread_info(event_source: dict) -> tuple[bool, str | None]:
    """Extract thread information from a Matrix event.

    Returns (is_thread, thread_id).
    """
    relates_to = event_source.get("content", {}).get("m.relates_to", {})
    is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
    thread_id = relates_to.get("event_id") if is_thread else None
    return is_thread, thread_id


def _maybe_ssl_context(homeserver: str) -> ssl_module.SSLContext | None:
    if homeserver.startswith("https://"):
        if os.getenv("MATRIX_SSL_VERIFY", "true").lower() == "false":
            # Create context that disables verification for dev/self-signed certs
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            # Use default context with proper verification
            ssl_context = ssl_module.create_default_context()
        return ssl_context
    return None


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
    ssl_context = _maybe_ssl_context(homeserver)
    if access_token:
        client = nio.AsyncClient(homeserver, user_id, store_path=".nio_store", ssl=ssl_context)
        client.access_token = access_token
    else:
        client = nio.AsyncClient(homeserver, user_id, ssl=ssl_context)

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
    ssl_context = _maybe_ssl_context(homeserver)
    client = nio.AsyncClient(homeserver, user_id, ssl=ssl_context)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        logger.info(f"Successfully logged in: {user_id}")
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise ValueError(msg)


async def register_user(
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
) -> str:
    """Register a new Matrix user account.

    Args:
        homeserver: The Matrix homeserver URL
        username: The username for the Matrix account (without domain)
        password: The password for the account
        display_name: The display name for the user

    Returns:
        The full Matrix user ID (e.g., @user:localhost)

    Raises:
        ValueError: If registration fails

    """
    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = MatrixID.from_username(username, server_name).full_id

    async with matrix_client(homeserver) as client:
        # Try to register the user
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom_agent",
        )

        if isinstance(response, nio.RegisterResponse):
            logger.info(f"✅ Successfully registered user: {user_id}")
            # After registration, we already have an access token
            client.user_id = response.user_id
            client.access_token = response.access_token
            client.device_id = response.device_id

            # Set display name using the existing session
            display_response = await client.set_displayname(display_name)
            if isinstance(display_response, nio.ErrorResponse):
                logger.warning(f"Failed to set display name: {display_response}")

            return user_id
        if isinstance(response, nio.ErrorResponse) and response.status_code == "M_USER_IN_USE":
            logger.info(f"User {user_id} already exists")
            return user_id
        msg = f"Failed to register user {username}: {response}"
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

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to send the message to
        content: The message content dictionary

    Returns:
        The event ID of the sent message, or None if sending failed

    """
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


def _extract_message_data(event: nio.RoomMessageText) -> dict[str, Any]:
    """Extract message data from a RoomMessageText event."""
    return {
        "sender": event.sender,
        "body": event.body,
        "timestamp": event.server_timestamp,
        "event_id": event.event_id,
        "content": event.source.get("content", {}),
    }


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
    messages = []
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

        thread_messages_found = 0
        for event in response.chunk:
            if isinstance(event, nio.RoomMessageText):
                if event.event_id == thread_id and not root_message_found:
                    messages.append(_extract_message_data(event))
                    root_message_found = True
                    thread_messages_found += 1
                else:
                    relates_to = event.source.get("content", {}).get("m.relates_to", {})
                    if relates_to.get("rel_type") == "m.thread" and relates_to.get("event_id") == thread_id:
                        messages.append(_extract_message_data(event))
                        thread_messages_found += 1

        if not response.end or thread_messages_found == 0:
            break
        from_token = response.end

    return list(reversed(messages))  # Return in chronological order


def markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML for Matrix formatted messages.

    Args:
        text: The markdown text to convert

    Returns:
        HTML formatted text

    """
    # Configure markdown with common extensions
    md = markdown.Markdown(
        extensions=[
            "markdown.extensions.fenced_code",
            "markdown.extensions.codehilite",
            "markdown.extensions.tables",
            "markdown.extensions.nl2br",
        ],
        extension_configs={
            "markdown.extensions.codehilite": {
                "use_pygments": True,  # Don't use pygments for syntax highlighting
                "noclasses": True,  # Use inline styles instead of CSS classes
            },
        },
    )
    html_text: str = md.convert(text)
    return html_text


async def edit_message(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
) -> str | None:
    """Edit an existing Matrix message.

    Args:
        client: The Matrix client
        room_id: The room ID where the message is
        event_id: The event ID of the message to edit
        new_content: The new content dictionary (from create_mention_content_from_text)
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

    return await send_message(client, room_id, edit_content)


async def set_avatar_from_file(
    client: nio.AsyncClient,
    avatar_path: Path,
) -> bool:
    """Set a user's avatar from a local file.

    Args:
        client: Authenticated Matrix client
        avatar_path: Path to the avatar image file

    Returns:
        True if successful, False otherwise

    """
    if not avatar_path.exists():
        logger.warning(f"Avatar file not found: {avatar_path}")
        return False

    extension = avatar_path.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(extension, "image/png")

    with avatar_path.open("rb") as f:
        avatar_data = f.read()

    file_size = len(avatar_data)

    def data_provider(_upload_monitor: object, _unused_data: object) -> io.BytesIO:
        return io.BytesIO(avatar_data)

    upload_result = await client.upload(
        data_provider=data_provider,
        content_type=content_type,
        filename=avatar_path.name,
        filesize=file_size,
    )

    # nio returns tuple (response, error)
    if isinstance(upload_result, tuple):
        upload_response, error = upload_result
        if error:
            logger.error(f"Upload error: {error}")
            return False
    else:
        upload_response = upload_result

    if not isinstance(upload_response, nio.UploadResponse):
        logger.error(f"Failed to upload avatar: {upload_response}")
        return False

    if not upload_response.content_uri:
        logger.error("Upload response missing content_uri")
        return False

    avatar_url = upload_response.content_uri
    response = await client.set_avatar(avatar_url)

    if isinstance(response, nio.ProfileSetAvatarResponse):
        logger.info(f"✅ Successfully set avatar for {client.user_id}")
        return True

    logger.error(f"Failed to set avatar for {client.user_id}: {response}")
    return False


async def check_and_set_avatar(
    client: nio.AsyncClient,
    avatar_path: Path,
) -> bool:
    """Check if user has an avatar and set it if they don't.

    Args:
        client: Authenticated Matrix client
        avatar_path: Path to the avatar image file

    Returns:
        True if avatar was already set or successfully set, False otherwise

    """
    response = await client.get_profile(client.user_id)

    if isinstance(response, nio.ProfileGetResponse) and response.avatar_url:
        logger.debug(f"Avatar already set for {client.user_id}")
        return True

    return await set_avatar_from_file(client, avatar_path)
