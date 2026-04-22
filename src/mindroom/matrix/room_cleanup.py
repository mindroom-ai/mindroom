"""Room cleanup utilities for removing stale bot memberships from Matrix rooms.

With the new self-managing agent pattern, agents handle their own room
memberships. This module only handles cleanup of stale/orphaned bots.

DM rooms are preserved and not cleaned up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members
from mindroom.matrix.identity import MatrixID, agent_username_localpart
from mindroom.matrix.invited_rooms_store import invited_rooms_path, load_invited_rooms, should_persist_invited_rooms
from mindroom.matrix.rooms import is_dm_room
from mindroom.matrix.state import MatrixState, managed_account_usernames
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _get_all_known_bot_usernames(runtime_paths: RuntimePaths) -> set[str]:
    """Get all bot usernames that have ever been created (from matrix_state.yaml).

    Returns:
        Set of all known bot usernames

    """
    return {
        username
        for key, username in managed_account_usernames(runtime_paths).items()
        if key != INTERNAL_USER_ACCOUNT_KEY
    }


def _load_all_persisted_invited_rooms(
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, set[str]]:
    """Load persisted invited rooms for opted-in agents, keyed by bot username."""
    invited_rooms_by_bot: dict[str, set[str]] = {}

    for agent_name in config.agents:
        if not should_persist_invited_rooms(config, agent_name):
            continue

        rooms = load_invited_rooms(invited_rooms_path(runtime_paths.storage_root, agent_name))
        if rooms:
            invited_rooms_by_bot[agent_username_localpart(agent_name, runtime_paths)] = rooms

    return invited_rooms_by_bot


async def _cleanup_orphaned_bots_in_room(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    persisted_invited_rooms_by_bot: dict[str, set[str]] | None = None,
) -> list[str]:
    """Remove orphaned bots from a single room.

    When DM mode is enabled, actual DM rooms are skipped to preserve them.

    Args:
        client: An authenticated Matrix client with kick permissions
        room_id: The room to check
        config: Current configuration
        runtime_paths: Explicit runtime context for Matrix state and identity resolution
        persisted_invited_rooms_by_bot: Preloaded persisted invited rooms keyed by bot username

    Returns:
        List of bot usernames that were kicked

    """
    # Never evict bots from the root space — the router is the creator/admin
    # and no agents are explicitly configured for it, so every bot looks orphaned.
    state = MatrixState.load(runtime_paths=runtime_paths)
    if state.space_room_id and room_id == state.space_room_id:
        logger.debug("orphaned_bot_cleanup_skipped_root_space", room_id=room_id)
        return []

    # When DM mode is enabled, check if this is actually a DM room
    if await is_dm_room(client, room_id):
        logger.debug("orphaned_bot_cleanup_skipped_dm_room", room_id=room_id)
        return []

    # Get room members
    member_ids = await get_room_members(client, room_id)
    if not member_ids:
        logger.warning("orphaned_bot_cleanup_members_unavailable", room_id=room_id)
        return []

    # Get configured bots for this room
    configured_bots = config.get_configured_bots_for_room(room_id, runtime_paths)
    known_bot_usernames = _get_all_known_bot_usernames(runtime_paths)
    if persisted_invited_rooms_by_bot is None:
        persisted_invited_rooms_by_bot = _load_all_persisted_invited_rooms(config, runtime_paths)

    kicked_bots = []

    for user_id in member_ids:
        matrix_id = MatrixID.parse(user_id)

        # Check if this is a mindroom bot and shouldn't be in this room
        if matrix_id.username in known_bot_usernames and matrix_id.username not in configured_bots:
            if room_id in persisted_invited_rooms_by_bot.get(matrix_id.username, set()):
                logger.debug(
                    "orphaned_bot_cleanup_preserved_persisted_invited_room",
                    agent=matrix_id.username,
                    room_id=room_id,
                )
                continue

            logger.info(
                "orphaned_bot_found",
                agent=matrix_id.username,
                room_id=room_id,
                configured_bots=sorted(configured_bots),
            )

            # Kick the bot
            kick_response = await client.room_kick(room_id, user_id, reason="Bot no longer configured for this room")

            if isinstance(kick_response, nio.RoomKickResponse):
                logger.info("orphaned_bot_kicked", agent=matrix_id.username, room_id=room_id, user_id=user_id)
                kicked_bots.append(matrix_id.username)
            else:
                logger.error(
                    "orphaned_bot_kick_failed",
                    agent=matrix_id.username,
                    room_id=room_id,
                    user_id=user_id,
                    error=str(kick_response),
                )

    return kicked_bots


async def cleanup_all_orphaned_bots(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
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

    logger.info("orphaned_bot_cleanup_started", room_count=len(joined_rooms))
    persisted_invited_rooms_by_bot = _load_all_persisted_invited_rooms(config, runtime_paths)

    for room_id in joined_rooms:
        room_kicked = await _cleanup_orphaned_bots_in_room(
            client,
            room_id,
            config,
            runtime_paths,
            persisted_invited_rooms_by_bot,
        )
        if room_kicked:
            kicked_bots[room_id] = room_kicked

    # Summary
    total_kicked = sum(len(bots) for bots in kicked_bots.values())
    if total_kicked > 0:
        logger.info(
            "orphaned_bot_cleanup_completed",
            total_kicked=total_kicked,
            room_count=len(kicked_bots),
        )
    else:
        logger.info("No orphaned bots found in any room")

    return kicked_bots
