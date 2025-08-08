"""Room management utilities for managing bot membership in Matrix rooms."""

import nio

from .agent_config import load_config
from .logging_config import get_logger
from .matrix import MatrixID, extract_server_name_from_homeserver
from .matrix.state import MatrixState
from .models import Config

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
        if key.startswith("agent_"):
            account = state.accounts[key]
            bot_usernames.add(account.username)

    return bot_usernames


async def _cleanup_orphaned_bots_in_room(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
) -> list[str]:
    """Remove orphaned bots from a single room.

    Args:
        client: An authenticated Matrix client with kick permissions
        room_id: The room to check
        config: Current configuration

    Returns:
        List of bot usernames that were kicked
    """

    # Get room members
    members_response = await client.joined_members(room_id)
    if not isinstance(members_response, nio.JoinedMembersResponse):
        logger.warning(f"Failed to get members for room {room_id}")
        return []

    # Get configured bots for this room
    configured_bots = config.get_configured_bots_for_room(room_id)
    known_bot_usernames = _get_all_known_bot_usernames()

    kicked_bots = []

    # Check each member
    for user_id, _member_info in members_response.members.items():
        matrix_id = MatrixID.parse(user_id)

        # Check if this is a mindroom bot and shouldn't be in this room
        if matrix_id.username in known_bot_usernames and matrix_id.username not in configured_bots:
            logger.info(
                f"Found orphaned bot {matrix_id.username} in room {room_id} "
                f"(configured bots for this room: {configured_bots})"
            )

            # Kick the bot
            kick_response = await client.room_kick(room_id, user_id, reason="Bot no longer configured for this room")

            if isinstance(kick_response, nio.RoomKickResponse):
                logger.info(f"Kicked {matrix_id.username} from {room_id}")
                kicked_bots.append(matrix_id.username)
            else:
                logger.error(f"Failed to kick {matrix_id.username} from {room_id}: {kick_response}")

    return kicked_bots


async def cleanup_all_orphaned_bots(client: nio.AsyncClient) -> dict[str, list[str]]:
    """Remove all orphaned bots from all rooms the client has access to.

    This should be called by a user or bot with admin/moderator permissions
    in the rooms that need cleaning.

    Args:
        client: An authenticated Matrix client

    Returns:
        Dictionary mapping room IDs to lists of kicked bot usernames
    """
    # Get current configuration
    config = load_config()

    # Track what we're doing
    kicked_bots: dict[str, list[str]] = {}

    # Get all rooms the client is in
    joined_rooms_response = await client.joined_rooms()
    if not isinstance(joined_rooms_response, nio.JoinedRoomsResponse):
        logger.error(f"Failed to get joined rooms: {joined_rooms_response}")
        return kicked_bots

    logger.info(f"Checking {len(joined_rooms_response.rooms)} rooms for orphaned bots")

    for room_id in joined_rooms_response.rooms:
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


async def _invite_missing_bots_to_room(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
) -> list[str]:
    """Invite configured bots that are missing from a room.

    Args:
        client: An authenticated Matrix client with invite permissions
        room_id: The room to check
        config: Current configuration

    Returns:
        List of bot usernames that were invited
    """
    # Get room members
    members_response = await client.joined_members(room_id)
    if not isinstance(members_response, nio.JoinedMembersResponse):
        logger.warning(f"Failed to get members for room {room_id}")
        return []

    # Get current members
    current_members = {MatrixID.parse(user_id).username for user_id in members_response.members}

    # Get configured bots for this room
    configured_bots = config.get_configured_bots_for_room(room_id)

    # Find bots that should be in the room but aren't
    missing_bots = configured_bots - current_members

    invited_bots = []

    for bot_username in missing_bots:
        # Extract the actual name (remove mindroom_ prefix)
        entity_name = bot_username[9:] if bot_username.startswith("mindroom_") else bot_username

        # Construct the full Matrix ID
        server_name = extract_server_name_from_homeserver(client.homeserver)
        matrix_id = MatrixID.from_agent(entity_name, server_name)
        full_user_id = matrix_id.full_id

        logger.info(f"Inviting missing bot {bot_username} to room {room_id}")

        # Invite the bot
        invite_response = await client.room_invite(room_id, full_user_id)

        if isinstance(invite_response, nio.RoomInviteResponse):
            logger.info(f"Invited {bot_username} to {room_id}")
            invited_bots.append(bot_username)
        else:
            logger.error(f"Failed to invite {bot_username} to {room_id}: {invite_response}")

    return invited_bots


async def invite_all_missing_bots(client: nio.AsyncClient) -> dict[str, list[str]]:
    """Invite all missing bots to all rooms they should be in.

    This should be called by a user or bot with admin/moderator permissions
    in the rooms that need bots invited.

    Args:
        client: An authenticated Matrix client

    Returns:
        Dictionary mapping room IDs to lists of invited bot usernames
    """
    # Get current configuration
    config = load_config()

    # Track what we're doing
    invited_bots: dict[str, list[str]] = {}

    # Get all rooms the client is in
    joined_rooms_response = await client.joined_rooms()
    if not isinstance(joined_rooms_response, nio.JoinedRoomsResponse):
        logger.error(f"Failed to get joined rooms: {joined_rooms_response}")
        return invited_bots

    logger.info(f"Checking {len(joined_rooms_response.rooms)} rooms for missing bots")

    for room_id in joined_rooms_response.rooms:
        room_invited = await _invite_missing_bots_to_room(client, room_id, config)
        if room_invited:
            invited_bots[room_id] = room_invited

    # Summary
    total_invited = sum(len(bots) for bots in invited_bots.values())
    if total_invited > 0:
        logger.info(f"Invited {total_invited} missing bots to {len(invited_bots)} rooms")
    else:
        logger.info("No missing bots found in any room")

    return invited_bots
