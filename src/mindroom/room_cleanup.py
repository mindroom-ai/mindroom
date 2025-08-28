"""Room cleanup utilities for removing stale bot memberships from Matrix rooms.

With the new self-managing agent pattern, agents handle their own room
memberships. This module only handles cleanup of stale/orphaned bots.

DM rooms are preserved and not cleaned up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from .logging_config import get_logger
from .matrix.client import get_joined_rooms, get_room_members
from .matrix.identity import MatrixID
from .matrix.rooms import is_dm_room
from .matrix.state import MatrixState

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)


def _get_all_known_bot_usernames() -> set[str]:
    """Get all bot usernames that have ever been created (from matrix_state.yaml).

    Returns:
        Set of all known bot usernames

    """
    state = MatrixState.load()
    bot_usernames = set()

    # Get all agent accounts from state
    for key in state.accounts:
        # Skip the user account (agent_user is the human user, not a bot)
        if key.startswith("agent_") and key != "agent_user":
            account = state.accounts[key]
            bot_usernames.add(account.username)

    return bot_usernames


async def _cleanup_orphaned_bots_in_room(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
) -> list[str]:
    """Remove orphaned bots from a single room.

    When DM mode is enabled, actual DM rooms are skipped to preserve them.

    Args:
        client: An authenticated Matrix client with kick permissions
        room_id: The room to check
        config: Current configuration

    Returns:
        List of bot usernames that were kicked

    """
    # When DM mode is enabled, check if this is actually a DM room
    if await is_dm_room(client, room_id):
        logger.debug(f"Skipping DM room {room_id} cleanup")
        return []

    # Get room members
    member_ids = await get_room_members(client, room_id)
    if not member_ids:
        logger.warning(f"No members found or failed to get members for room {room_id}")
        return []

    # Get configured bots for this room
    configured_bots = config.get_configured_bots_for_room(room_id)
    known_bot_usernames = _get_all_known_bot_usernames()

    kicked_bots = []

    for user_id in member_ids:
        matrix_id = MatrixID.parse(user_id)

        # Check if this is a mindroom bot and shouldn't be in this room
        if matrix_id.username in known_bot_usernames and matrix_id.username not in configured_bots:
            logger.info(
                f"Found orphaned bot {matrix_id.username} in room {room_id} "
                f"(configured bots for this room: {configured_bots})",
            )

            # Kick the bot
            kick_response = await client.room_kick(room_id, user_id, reason="Bot no longer configured for this room")

            if isinstance(kick_response, nio.RoomKickResponse):
                logger.info(f"Kicked {matrix_id.username} from {room_id}")
                kicked_bots.append(matrix_id.username)
            else:
                logger.error(f"Failed to kick {matrix_id.username} from {room_id}: {kick_response}")

    return kicked_bots


async def cleanup_all_orphaned_bots(
    client: nio.AsyncClient,
    config: Config,
) -> dict[str, list[str]]:
    """Remove all orphaned bots from all rooms the client has access to.

    This should be called by a user or bot with admin/moderator permissions
    in the rooms that need cleaning.

    Returns:
        Dictionary mapping room IDs to lists of kicked bot usernames

    """
    # Track what we're doing
    kicked_bots: dict[str, list[str]] = {}

    # Get all rooms the client is in
    joined_rooms = await get_joined_rooms(client)
    if joined_rooms is None:
        return kicked_bots

    logger.info(f"Checking {len(joined_rooms)} rooms for orphaned bots")

    for room_id in joined_rooms:
        room_kicked = await _cleanup_orphaned_bots_in_room(client, room_id, config)
        if room_kicked:
            kicked_bots[room_id] = room_kicked

    # Summary
    total_kicked = sum(len(bots) for bots in kicked_bots.values())
    if total_kicked > 0:
        logger.info(f"Kicked {total_kicked} orphaned bots from {len(kicked_bots)} rooms")
    else:
        logger.info("No orphaned bots found in any room")

    return kicked_bots
