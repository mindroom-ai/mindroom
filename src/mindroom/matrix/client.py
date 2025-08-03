"""Matrix client operations and utilities."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import markdown
import nio

from ..logging_config import emoji, get_logger
from .users import extract_server_name_from_homeserver

logger = get_logger(__name__)


def extract_thread_info(event_source: dict) -> tuple[bool, str | None]:
    """Extract thread information from a Matrix event.

    Returns (is_thread, thread_id).
    """
    relates_to = event_source.get("content", {}).get("m.relates_to", {})
    is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
    thread_id = relates_to.get("event_id") if is_thread else None
    return is_thread, thread_id


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
    if access_token:
        client = nio.AsyncClient(homeserver, user_id, store_path=".nio_store")
        client.access_token = access_token
    else:
        client = nio.AsyncClient(homeserver, user_id)

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
    client = nio.AsyncClient(homeserver, user_id)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        logger.info(f"Successfully logged in: {user_id}")
        return client
    else:
        await client.close()
        raise ValueError(f"Failed to login {user_id}: {response}")


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
    user_id = f"@{username}:{server_name}"

    async with matrix_client(homeserver) as client:
        # Try to register the user
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom_agent",
        )

        if isinstance(response, nio.RegisterResponse):
            logger.info(f"Successfully registered user: {user_id}")
            # Set display name
            login_response = await client.login(password)
            if not isinstance(login_response, nio.LoginResponse):
                logger.error(f"Failed to login after registration: {login_response}")
                raise ValueError(f"Failed to login after registration: {login_response}")

            display_response = await client.set_displayname(display_name)
            if isinstance(display_response, nio.ErrorResponse):
                logger.warning(f"Failed to set display name: {display_response}")

            return user_id
        elif (
            isinstance(response, nio.ErrorResponse)
            and hasattr(response, "status_code")
            and response.status_code == "M_USER_IN_USE"
        ):
            logger.info(f"User {user_id} already exists")
            return user_id
        else:
            raise ValueError(f"Failed to register user {username}: {response}")


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
    else:
        logger.error(f"Failed to invite {user_id} to room {room_id}: {response}")
        return False


async def create_room(
    client: nio.AsyncClient,
    name: str,
    alias: str | None = None,
    topic: str | None = None,
) -> str | None:
    """Create a new Matrix room.

    Args:
        client: Authenticated Matrix client
        name: Room name
        alias: Optional room alias (without # and domain)
        topic: Optional room topic

    Returns:
        Room ID if successful, None otherwise
    """
    room_config = {"name": name}
    if alias:
        room_config["room_alias_name"] = alias
    if topic:
        room_config["topic"] = topic

    response = await client.room_create(**room_config)
    if isinstance(response, nio.RoomCreateResponse):
        logger.info(f"Created room: {name} ({response.room_id})")
        return str(response.room_id)
    else:
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
    else:
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
    else:
        logger.warning(f"Could not check members for room {room_id}")
        return set()


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

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=100,
            message_filter={"types": ["m.room.message"]},
            direction=nio.MessageDirection.back,
        )

        if not hasattr(response, "chunk"):
            logger.error("Failed to fetch thread history", room_id=room_id, error=str(response))
            break

        # Break if no new messages found
        if not response.chunk:
            break

        thread_messages_found = 0
        for event in response.chunk:
            if hasattr(event, "source") and event.source.get("type") == "m.room.message":
                relates_to = event.source.get("content", {}).get("m.relates_to", {})
                if relates_to.get("rel_type") == "m.thread" and relates_to.get("event_id") == thread_id:
                    messages.append(
                        {
                            "sender": event.sender,
                            "body": getattr(event, "body", ""),
                            "timestamp": event.server_timestamp,
                            "event_id": event.event_id,
                        },
                    )
                    thread_messages_found += 1

        # Exit if we've reached the end or no more relevant messages
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
            }
        },
    )
    html_text: str = md.convert(text)
    return html_text


def prepare_response_content(
    response_text: str,
    event: nio.RoomMessageText,
    agent_name: str = "",
) -> dict[str, Any]:
    """Prepares the content for the response message.

    Args:
        response_text: The text to send
        event: The event being responded to
        agent_name: Optional agent name for logging

    Returns:
        Message content dictionary
    """
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": response_text,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(response_text),
    }

    is_thread_reply, thread_id = extract_thread_info(event.source)
    relates_to = event.source.get("content", {}).get("m.relates_to")

    agent_prefix = emoji(agent_name) if agent_name else ""

    logger.debug(
        f"{agent_prefix} Preparing response content - Original event_id: {event.event_id}, "
        f"Original relates_to: {relates_to}, Is thread reply: {is_thread_reply}"
    )

    if relates_to:
        if is_thread_reply:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": relates_to.get("event_id"),
                "m.in_reply_to": {"event_id": event.event_id},
            }
            logger.debug(f"{agent_prefix} Setting thread reply with thread_id: {relates_to.get('event_id')}")
        else:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
            logger.debug(f"{agent_prefix} Setting regular reply (not thread)")
    else:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": event.event_id}}
        logger.debug(f"{agent_prefix} No relates_to in original message, setting regular reply")

    logger.debug(f"{agent_prefix} Final content m.relates_to: {content.get('m.relates_to')}")

    return content
