"""Room cleanup utilities for removing stale bot memberships from Matrix rooms.

With the new self-managing agent pattern, agents handle their own room
memberships. This module only handles cleanup of stale/orphaned bots.

DM rooms are preserved and not cleaned up.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio

from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.rooms import is_dm_room
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _get_all_known_bot_user_ids(config: Config, runtime_paths: RuntimePaths) -> set[str]:
    """Get all current persisted bot Matrix user IDs from matrix_state.yaml."""
    domain = config.get_domain(runtime_paths)
    state = matrix_state_for_runtime(runtime_paths)
    return {
        MatrixID.from_username(account.username, account.domain or domain).full_id
        for key, account in state.accounts.items()
        if key.startswith("agent_")
        if key != INTERNAL_USER_ACCOUNT_KEY
    }


async def _cleanup_orphaned_bots_in_room(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Remove orphaned bots from a single room.

    When DM mode is enabled, actual DM rooms are skipped to preserve them.

    Args:
        client: An authenticated Matrix client with kick permissions
        room_id: The room to check
        config: Current configuration
        runtime_paths: Explicit runtime context for Matrix state and identity resolution

    Returns:
        List of bot Matrix user IDs that were removed from the room

    """
    # Root-space membership is managed separately from ordinary room cleanup.
    state = matrix_state_for_runtime(runtime_paths)
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

    known_bot_user_ids = _get_all_known_bot_user_ids(config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)

    removed_bots = []

    # Sweep the client's own account last — once it leaves the room it can no
    # longer kick the remaining orphans.
    for user_id in sorted(member_ids, key=lambda member_id: member_id == client.user_id):
        matrix_id = MatrixID.parse(user_id)
        agent_name = registry.current_entity_name_for_user_id(user_id)
        # Current bots reconcile their own memberships after startup hooks and
        # pending invitations. This earlier sweep only owns retired identities.
        if user_id in known_bot_user_ids and agent_name is None:
            logger.info(
                "orphaned_bot_found",
                agent=matrix_id.username,
                user_id=user_id,
                room_id=room_id,
            )

            if await _remove_orphaned_bot(client, room_id, matrix_id):
                removed_bots.append(user_id)

    return removed_bots


async def _remove_orphaned_bot(client: nio.AsyncClient, room_id: str, matrix_id: MatrixID) -> bool:
    """Remove one orphaned bot from a room, returning True on success.

    Matrix forbids kicking your own account (M_FORBIDDEN), so when the orphan
    is the sweeping client itself it leaves the room instead of kicking.
    """
    user_id = matrix_id.full_id
    if user_id == client.user_id:
        leave_response = await client.room_leave(room_id)
        if isinstance(leave_response, nio.RoomLeaveResponse):
            logger.info("orphaned_bot_left", agent=matrix_id.username, room_id=room_id, user_id=user_id)
            return True
        logger.error(
            "orphaned_bot_leave_failed",
            agent=matrix_id.username,
            room_id=room_id,
            user_id=user_id,
            error=str(leave_response),
        )
        return False

    kick_response = await client.room_kick(room_id, user_id, reason="Bot no longer configured for this room")
    if isinstance(kick_response, nio.RoomKickResponse):
        logger.info("orphaned_bot_kicked", agent=matrix_id.username, room_id=room_id, user_id=user_id)
        return True
    logger.error(
        "orphaned_bot_kick_failed",
        agent=matrix_id.username,
        room_id=room_id,
        user_id=user_id,
        error=str(kick_response),
    )
    return False


async def cleanup_all_orphaned_bots(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, list[str]]:
    """Remove retired bot identities from all rooms the client has access to.

    This should be called by a user or bot with admin/moderator permissions
    in the rooms that need cleaning.
    Configured entities manage their own memberships, even if startup has not
    completed or their invited-room retention records have not been restored.

    Returns:
        Dictionary mapping room IDs to lists of removed bot Matrix user IDs

    """
    # Track what we're doing
    kicked_bots: dict[str, list[str]] = {}

    # Get all rooms the client is in
    joined_rooms = await get_joined_rooms(client)
    if joined_rooms is None:
        return kicked_bots

    logger.info("orphaned_bot_cleanup_started", room_count=len(joined_rooms))

    # Only independent rooms overlap. Each room still kicks other orphans
    # before leaving itself, and cancellation drains all workers before return.
    pending_rooms = iter(joined_rooms)

    async def clean_rooms() -> None:
        for room_id in pending_rooms:
            room_kicked = await _cleanup_orphaned_bots_in_room(
                client,
                room_id,
                config,
                runtime_paths,
            )
            if room_kicked:
                kicked_bots[room_id] = room_kicked

    async with asyncio.TaskGroup() as workers:
        for _ in range(min(4, len(joined_rooms))):
            workers.create_task(clean_rooms())

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
