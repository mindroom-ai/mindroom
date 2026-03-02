"""Matrix room management functions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.avatar import check_and_set_avatar
from mindroom.matrix.client import (
    create_room,
    ensure_room_directory_visibility,
    ensure_room_join_rule,
    join_room,
    leave_room,
    matrix_client,
)
from mindroom.matrix.identity import MatrixID, extract_server_name_from_homeserver, managed_room_alias_localpart
from mindroom.matrix.state import MatrixRoom, MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.topic_generator import ensure_room_has_topic, generate_room_topic_ai

if TYPE_CHECKING:
    from mindroom.config.main import Config

logger = get_logger(__name__)


async def _configure_managed_room_access(
    client: nio.AsyncClient,
    room_key: str,
    room_id: str,
    config: Config,
    *,
    room_alias: str | None = None,
    context: str,
) -> bool:
    """Apply configured room joinability/discoverability policy for one managed room."""
    access_config = config.matrix_room_access
    target_join_rule = access_config.get_target_join_rule(room_key, room_id=room_id, room_alias=room_alias)
    target_directory_visibility = access_config.get_target_directory_visibility(
        room_key,
        room_id=room_id,
        room_alias=room_alias,
    )

    if target_join_rule is None or target_directory_visibility is None:
        logger.info(
            "Skipping managed room access policy",
            room_key=room_key,
            room_id=room_id,
            mode=access_config.mode,
            reason="single_user_private mode keeps invite-only/private behavior",
            context=context,
        )
        return True

    logger.info(
        "Applying managed room access policy",
        room_key=room_key,
        room_id=room_id,
        join_rule=target_join_rule,
        directory_visibility=target_directory_visibility,
        publish_to_room_directory=access_config.publish_to_room_directory,
        context=context,
    )

    join_rule_updated = await ensure_room_join_rule(client, room_id, target_join_rule)
    directory_visibility_updated = await ensure_room_directory_visibility(client, room_id, target_directory_visibility)
    if join_rule_updated and directory_visibility_updated:
        return True

    logger.warning(
        "Managed room access policy was only partially applied",
        room_key=room_key,
        room_id=room_id,
        join_rule_success=join_rule_updated,
        directory_visibility_success=directory_visibility_updated,
        context=context,
    )
    return False


def _room_key_to_name(room_key: str) -> str:
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


def _get_room_id(room_key: str) -> str | None:
    """Get room ID for a given room key/alias."""
    state = MatrixState.load()
    room = state.get_room(room_key)
    return room.room_id if room else None


def add_room(room_key: str, room_id: str, alias: str, name: str) -> None:
    """Add a new room to the state."""
    state = MatrixState.load()
    state.add_room(room_key, room_id, alias, name)
    state.save()


def _remove_room(room_key: str) -> bool:
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


async def _ensure_room_exists(  # noqa: C901, PLR0912
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
    alias_localpart = managed_room_alias_localpart(room_key)
    full_alias = f"#{alias_localpart}:{server_name}"

    response = await client.room_resolve_alias(full_alias)
    if isinstance(response, nio.RoomResolveAliasResponse):
        room_id = response.room_id
        logger.debug(f"Room alias {full_alias} exists on server, room ID: {room_id}")

        # Update our state if needed
        if room_key not in existing_rooms or existing_rooms[room_key].room_id != room_id:
            if room_name is None:
                room_name = _room_key_to_name(room_key)
            add_room(room_key, room_id, full_alias, room_name)
            logger.info(f"Updated state with existing room {room_key} (ID: {room_id})")

        # Try to join the room
        joined_room = await join_room(client, room_id)
        if joined_room:
            # For existing rooms, ensure they have a topic set
            if room_name is None:
                room_name = _room_key_to_name(room_key)
            await ensure_room_has_topic(client, room_id, room_key, room_name, config)

            if config.matrix_room_access.is_multi_user_mode() and config.matrix_room_access.reconcile_existing_rooms:
                await _configure_managed_room_access(
                    client=client,
                    room_key=room_key,
                    room_id=str(room_id),
                    config=config,
                    room_alias=full_alias,
                    context="existing_room_reconciliation",
                )
            elif config.matrix_room_access.is_multi_user_mode():
                logger.info(
                    "Skipping existing room access reconciliation",
                    room_key=room_key,
                    room_id=str(room_id),
                    reason="matrix_room_access.reconcile_existing_rooms is false",
                )
        else:
            msg = (
                f"Managed room alias '{full_alias}' already exists as '{room_id}' but this MindRoom could not join it. "
                "Possible causes: another installation owns this alias, the room is invite-only, or server-side access policies prevent joining. "
                "If on a shared homeserver, try setting a unique MINDROOM_NAMESPACE."
            )
            raise RuntimeError(msg)
        return str(room_id)

    # Room alias doesn't exist on server, so we can create it
    if room_key in existing_rooms:
        # Remove stale entry from state
        logger.debug(f"Removing stale room {room_key} from state")
        _remove_room(room_key)

    # Create the room
    if room_name is None:
        room_name = _room_key_to_name(room_key)

    # Generate a contextual topic for the room using AI
    topic = await generate_room_topic_ai(room_key, room_name, config)
    logger.info(f"Creating room {room_key} with topic: {topic}")

    created_room_id = await create_room(
        client=client,
        name=room_name,
        alias=alias_localpart,
        topic=topic,
        power_users=power_users or [],
    )

    if created_room_id:
        # Save room info
        add_room(room_key, created_room_id, full_alias, room_name)
        logger.info(f"Created room {room_key} with ID {created_room_id}")

        if config.matrix_room_access.is_multi_user_mode():
            await _configure_managed_room_access(
                client=client,
                room_key=room_key,
                room_id=created_room_id,
                config=config,
                room_alias=full_alias,
                context="new_room_creation",
            )
        else:
            logger.info(
                "Created room with single-user/private defaults",
                room_key=room_key,
                room_id=created_room_id,
                mode=config.matrix_room_access.mode,
                join_rule="invite",
                directory_visibility="private",
            )

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
        try:
            room_id = await _ensure_room_exists(
                client=client,
                room_key=room_key,
                config=config,
                power_users=power_users,
            )
        except RuntimeError:
            logger.exception(
                "Failed to ensure managed room; continuing with remaining rooms",
                room_key=room_key,
            )
            continue

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
    user_account = state.get_account(INTERNAL_USER_ACCOUNT_KEY)
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


_DM_ROOM_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}
_DIRECT_ROOMS_CACHE: dict[str, tuple[float, set[str]]] = {}
_DM_ROOM_TTL: float = 300  # seconds
_DIRECT_ROOMS_TTL: float = 300  # seconds


def _dm_cache_key(client: nio.AsyncClient, room_id: str) -> tuple[str, str]:
    """Build a cache key that is scoped per user.

    DM membership via ``m.direct`` is account-specific, so room-only cache keys
    can leak incorrect results between different bot users.
    """
    return (str(client.user_id or ""), room_id)


async def _get_direct_room_ids(client: nio.AsyncClient) -> set[str]:
    """Get DM room IDs from the user's ``m.direct`` account data.

    Results are cached per user for ``_DIRECT_ROOMS_TTL`` seconds so that
    newly created DM rooms are picked up without a restart.
    """
    user_id = str(client.user_id or "")
    if not user_id:
        return set()

    cached = _DIRECT_ROOMS_CACHE.get(user_id)
    if cached is not None:
        ts, room_ids = cached
        if time.monotonic() - ts < _DIRECT_ROOMS_TTL:
            return room_ids

    response = await client.list_direct_rooms()
    if isinstance(response, nio.DirectRoomsResponse):
        direct_room_ids = {room_id for room_ids in response.rooms.values() for room_id in room_ids}
        _DIRECT_ROOMS_CACHE[user_id] = (time.monotonic(), direct_room_ids)
        return direct_room_ids
    if isinstance(response, nio.DirectRoomsErrorResponse) and response.status_code == "M_NOT_FOUND":
        # No m.direct account data is a stable empty state for this user.
        _DIRECT_ROOMS_CACHE[user_id] = (time.monotonic(), set())

    return set()


def _is_two_member_group_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Check if nio models this room as an unnamed two-member group.

    Rooms with an explicit topic are excluded because DMs almost never have one,
    while small project rooms often do.
    """
    room_lookup = getattr(client, "rooms", None)
    if not isinstance(room_lookup, dict):
        return False

    room = room_lookup.get(room_id)
    if room is None or not room.is_group or room.member_count != 2:
        return False
    return not room.topic


def _has_is_direct_marker(state_events: list[dict[str, Any]]) -> bool:
    """Check ``m.room.member`` state events for the ``is_direct`` flag."""
    for event in state_events:
        if event.get("type") != "m.room.member":
            continue

        content = event.get("content")
        if isinstance(content, dict) and content.get("is_direct") is True:
            return True

    return False


async def is_dm_room(client: nio.AsyncClient, room_id: str) -> bool:
    """Check if a room is a Direct Message (DM) room.

    Detection uses multiple signals in this order:
    1. ``m.direct`` account data (via ``/account_data/m.direct``)
    2. Nio's in-memory room model for 2-member ad-hoc rooms
    3. Room state events with ``is_direct=true``

    Args:
        client: The Matrix client
        room_id: The room ID to check

    Returns:
        True if the room is a DM room, False otherwise

    """
    cache_key = _dm_cache_key(client, room_id)
    cached = _DM_ROOM_CACHE.get(cache_key)
    if cached is not None:
        ts, is_dm = cached
        if time.monotonic() - ts < _DM_ROOM_TTL:
            return is_dm

    direct_room_ids = await _get_direct_room_ids(client)
    if room_id in direct_room_ids:
        _DM_ROOM_CACHE[cache_key] = (time.monotonic(), True)
        return True

    # Preserve DM-like rooms even when servers don't expose `is_direct` in state.
    if _is_two_member_group_room(client, room_id):
        _DM_ROOM_CACHE[cache_key] = (time.monotonic(), True)
        return True

    # Get the room state events, specifically member events.
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return False

    is_dm = _has_is_direct_marker(response.events)
    _DM_ROOM_CACHE[cache_key] = (time.monotonic(), is_dm)
    return is_dm


async def leave_non_dm_rooms(
    client: nio.AsyncClient,
    room_ids: list[str],
) -> None:
    """Leave all rooms in *room_ids* that are not DM rooms."""
    for room_id in room_ids:
        if await is_dm_room(client, room_id):
            logger.debug(f"Preserving DM room {room_id}")
            continue
        success = await leave_room(client, room_id)
        if success:
            logger.info(f"Left room {room_id}")
        else:
            logger.error(f"Failed to leave room {room_id}")
