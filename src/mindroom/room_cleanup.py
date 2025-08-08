"""Room cleanup utilities for removing orphaned bots from Matrix rooms."""

import nio

from .agent_config import ROUTER_AGENT_NAME, load_config
from .logging_config import get_logger
from .matrix import MatrixID, resolve_room_aliases
from .matrix.state import MatrixState
from .models import Config

logger = get_logger(__name__)


def _get_configured_bots_for_room(config: Config, room_id: str) -> set[str]:
    """Get the set of bot usernames that should be in a specific room.

    Args:
        config: The current configuration
        room_id: The Matrix room ID

    Returns:
        Set of bot usernames (without domain) that should be in this room
    """
    configured_bots = set()

    # Check which agents should be in this room
    for agent_name, agent_config in config.agents.items():
        resolved_rooms = resolve_room_aliases(agent_config.rooms)
        if room_id in resolved_rooms:
            configured_bots.add(f"mindroom_{agent_name}")

    # Check which teams should be in this room
    for team_name, team_config in config.teams.items():
        resolved_rooms = resolve_room_aliases(team_config.rooms)
        if room_id in resolved_rooms:
            configured_bots.add(f"mindroom_{team_name}")

    # Router should be in any room that has any configured agents/teams
    all_configured_rooms = set()
    for agent_config in config.agents.values():
        all_configured_rooms.update(resolve_room_aliases(agent_config.rooms))
    for team_config in config.teams.values():
        all_configured_rooms.update(resolve_room_aliases(team_config.rooms))

    if room_id in all_configured_rooms:
        configured_bots.add(f"mindroom_{ROUTER_AGENT_NAME}")

    return configured_bots


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
    configured_bots = _get_configured_bots_for_room(config, room_id)
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
