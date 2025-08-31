"""Matrix room management functions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.topic_generator import ensure_room_has_topic, generate_room_topic_ai

from .client import check_and_set_avatar, create_room, join_room, matrix_client
from .identity import MatrixID, extract_server_name_from_homeserver
from .state import MatrixRoom, MatrixState

if TYPE_CHECKING:
    from mindroom.config import Config

logger = get_logger(__name__)


def room_key_to_name(room_key: str) -> str:
    """Convert a room key to a human-readable room name.

    Args:
        room_key: The room key (e.g., 'dev', 'analysis_room')

    Returns:
        Human-readable room name (e.g., 'Dev', 'Analysis Room')

    """
    return room_key.replace("_", " ").title()


def load_rooms() -> dict[str, MatrixRoom]:
    """Load room state from YAML file."""
    state = MatrixState.load()
    return state.rooms


def get_room_aliases() -> dict[str, str]:
    """Get mapping of room aliases to room IDs."""
    state = MatrixState.load()
    return state.get_room_aliases()


def get_room_id(room_key: str) -> str | None:
    """Get room ID for a given room key/alias."""
    state = MatrixState.load()
    room = state.get_room(room_key)
    return room.room_id if room else None


def add_room(room_key: str, room_id: str, alias: str, name: str) -> None:
    """Add a new room to the state."""
    state = MatrixState.load()
    state.add_room(room_key, room_id, alias, name)
    state.save()


def remove_room(room_key: str) -> bool:
    """Remove a room from the state."""
    state = MatrixState.load()
    if room_key in state.rooms:
        del state.rooms[room_key]
        state.save()
        return True
    return False


def resolve_room_aliases(room_list: list[str]) -> list[str]:
    """Resolve room aliases to room IDs.

    Args:
        room_list: List of room aliases or IDs

    Returns:
        List of room IDs (aliases resolved to IDs, IDs passed through)

    """
    room_aliases = get_room_aliases()
    return [room_aliases.get(room, room) for room in room_list]


def get_room_alias_from_id(room_id: str) -> str | None:
    """Get room alias from room ID (reverse lookup).

    Args:
        room_id: Matrix room ID

    Returns:
        Room alias if found, None otherwise

    """
    room_aliases = get_room_aliases()
    for alias, rid in room_aliases.items():
        if rid == room_id:
            return alias
    return None


async def ensure_room_exists(  # noqa: C901
    client: nio.AsyncClient,
    room_key: str,
    config: Config,
    room_name: str | None = None,
    power_users: list[str] | None = None,
) -> str | None:
    """Ensure a room exists, creating it if necessary.

    Args:
        client: Matrix client to use for room creation
        room_key: The room key/alias (without domain)
        config: Configuration with agent settings for topic generation
        room_name: Display name for the room (defaults to room_key with underscores replaced)
        power_users: List of user IDs to grant power levels to

    Returns:
        Room ID if room exists or was created, None on failure

    """
    existing_rooms = load_rooms()

    # First, try to resolve the room alias on the server
    # This handles cases where the room exists on server but not in our state
    server_name = extract_server_name_from_homeserver(client.homeserver)
    full_alias = f"#{room_key}:{server_name}"

    response = await client.room_resolve_alias(full_alias)
    if isinstance(response, nio.RoomResolveAliasResponse):
        room_id = response.room_id
        logger.debug(f"Room alias {full_alias} exists on server, room ID: {room_id}")

        # Update our state if needed
        if room_key not in existing_rooms or existing_rooms[room_key].room_id != room_id:
            if room_name is None:
                room_name = room_key_to_name(room_key)
            add_room(room_key, room_id, full_alias, room_name)
            logger.info(f"Updated state with existing room {room_key} (ID: {room_id})")

        # Try to join the room
        if await join_room(client, room_id):
            # For existing rooms, ensure they have a topic set
            if room_name is None:
                room_name = room_key_to_name(room_key)
            await ensure_room_has_topic(client, room_id, room_key, room_name, config)
            return str(room_id)
        # Room exists but we can't join - this means the room was created
        # but this user isn't a member. Return the room ID anyway since
        # the room does exist and invitations will be handled separately
        logger.debug(f"Room {room_key} exists but user not a member, returning room ID for invitation handling")
        return str(room_id)

    # Room alias doesn't exist on server, so we can create it
    if room_key in existing_rooms:
        # Remove stale entry from state
        logger.debug(f"Removing stale room {room_key} from state")
        remove_room(room_key)

    # Create the room
    if room_name is None:
        room_name = room_key_to_name(room_key)

    # Generate a contextual topic for the room using AI
    topic = await generate_room_topic_ai(room_key, room_name, config)
    logger.info(f"Creating room {room_key} with topic: {topic}")

    created_room_id = await create_room(
        client=client,
        name=room_name,
        alias=room_key,
        topic=topic,
        power_users=power_users or [],
    )

    if created_room_id:
        # Save room info
        add_room(room_key, created_room_id, full_alias, room_name)
        logger.info(f"Created room {room_key} with ID {created_room_id}")

        # Set room avatar if available (for newly created rooms)
        # Note: Avatars can also be updated later using scripts/generate_avatars.py
        avatar_path = Path(__file__).parent.parent.parent.parent / "avatars" / "rooms" / f"{room_key}.png"
        if avatar_path.exists():
            if await check_and_set_avatar(client, avatar_path, room_id=created_room_id):
                logger.info(f"Set avatar for newly created room {room_key}")
            else:
                logger.warning(f"Failed to set avatar for room {room_key}")

        return created_room_id
    logger.error(f"Failed to create room {room_key}")
    return None


async def ensure_all_rooms_exist(
    client: nio.AsyncClient,
    config: Config,
) -> dict[str, str]:
    """Ensure all configured rooms exist and invite user account.

    Args:
        client: Matrix client to use for room creation
        config: Configuration with room settings

    Returns:
        Dict mapping room keys to room IDs

    """
    from mindroom.agents import get_agent_ids_for_room  # noqa: PLC0415

    room_ids = {}

    # Get all configured rooms
    all_rooms = config.get_all_configured_rooms()

    for room_key in all_rooms:
        # Skip if this is a room ID (starts with !)
        if room_key.startswith("!"):
            # This is a room ID, not a room key/alias - skip it
            continue

        # Get power users for this room
        power_users = get_agent_ids_for_room(room_key, config)

        # Ensure room exists
        room_id = await ensure_room_exists(
            client=client,
            room_key=room_key,
            config=config,
            power_users=power_users,
        )

        if room_id:
            room_ids[room_key] = room_id

    return room_ids


async def ensure_user_in_rooms(homeserver: str, room_ids: dict[str, str]) -> None:
    """Ensure the user account is a member of all specified rooms.

    Args:
        homeserver: Matrix homeserver URL
        room_ids: Dict mapping room keys to room IDs

    """
    state = MatrixState.load()
    # User account is stored as "agent_user" (treated as a special agent)
    user_account = state.get_account("agent_user")
    if not user_account:
        logger.warning("No user account found, skipping user room membership")
        return

    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = MatrixID.from_username(user_account.username, server_name).full_id

    # Create a client for the user to join rooms
    async with matrix_client(homeserver, user_id) as user_client:
        # Login as the user
        login_response = await user_client.login(password=user_account.password)
        if not isinstance(login_response, nio.LoginResponse):
            logger.error(f"Failed to login as user {user_id}: {login_response}")
            return

        logger.info(f"User {user_id} logged in to join rooms")

        for room_key, room_id in room_ids.items():
            # Try to join the room (will work if invited or room is public)
            join_success = await join_room(user_client, room_id)
            if join_success:
                logger.info(f"User {user_id} joined room {room_key}")
            else:
                logger.warning(f"User {user_id} failed to join room {room_key} - may need invitation")


DM_ROOM_CACHE: dict[str, bool] = {}


async def is_dm_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Check if a room is a Direct Message (DM) room.

    DM rooms have the "is_direct" flag set to true in member state events.
    This function checks the room state to determine if it's a DM.

    Args:
        client: The Matrix client
        room_id: The room ID to check

    Returns:
        True if the room is a DM room, False otherwise

    """
    if room_id in DM_ROOM_CACHE:
        return DM_ROOM_CACHE[room_id]
    # Get the room state events, specifically member events
    response = await client.room_get_state(room_id)

    if isinstance(response, nio.RoomGetStateResponse):
        # Check member events for the is_direct flag
        for event in response.events:
            if event.get("type") == "m.room.member":
                content = event.get("content", {})
                if content.get("is_direct") is True:
                    # Cache the result for this room ID
                    DM_ROOM_CACHE[room_id] = True
                    return True

    # If we can't find is_direct=true in any member event, it's not a DM
    DM_ROOM_CACHE[room_id] = False
    return False
