"""Room membership management for Matrix bots."""

import nio

from .agent_config import ROUTER_AGENT_NAME, load_config
from .logging_config import get_logger
from .matrix import MATRIX_HOMESERVER, MatrixID, extract_server_name_from_homeserver, login_agent_user

logger = get_logger(__name__)


def get_all_mindroom_user_ids(homeserver: str) -> set[str]:
    """Get all possible mindroom user IDs based on the naming pattern.

    Args:
        homeserver: The Matrix homeserver URL

    Returns:
        Set of Matrix user IDs that belong to mindroom bots
    """
    server_name = extract_server_name_from_homeserver(homeserver)

    # We need to get all users that match our pattern: @mindroom_*:server
    # Since we can't query all users from Matrix, we need to build this from config
    # and known patterns

    config = load_config()
    user_ids = set()

    # Add router
    user_ids.add(MatrixID.from_agent(ROUTER_AGENT_NAME, server_name).full_id)

    # Add all configured agents
    for agent_name in config.agents:
        user_ids.add(MatrixID.from_agent(agent_name, server_name).full_id)

    # Add all configured teams
    for team_name in config.teams:
        user_ids.add(MatrixID.from_agent(team_name, server_name).full_id)

    return user_ids


async def get_all_existing_mindroom_users(homeserver: str) -> dict[str, nio.AsyncClient]:
    """Get all existing mindroom Matrix users and log them in.

    This function tries to log in all possible mindroom users to see which ones
    actually exist on the server.

    Args:
        homeserver: The Matrix homeserver URL

    Returns:
        Dictionary mapping user IDs to logged-in clients
    """
    from .matrix import AgentMatrixUser

    server_name = extract_server_name_from_homeserver(homeserver)
    existing_users = {}

    # Try to login known patterns of users
    # We'll check a reasonable set of common agent names plus configured ones
    config = load_config()

    # Collect all possible agent names
    possible_names = {ROUTER_AGENT_NAME}
    possible_names.update(config.agents.keys())
    possible_names.update(config.teams.keys())

    # Also check for some common names that might have been used before
    # This helps catch orphaned bots from previous configurations
    common_names = {
        "general",
        "calculator",
        "code",
        "shell",
        "summary",
        "research",
        "finance",
        "news",
        "data_analyst",
        "security",
        "analyst",
        "super_team",
        "dev_team",
        "research_team",
    }
    possible_names.update(common_names)

    for name in possible_names:
        user_id = MatrixID.from_agent(name, server_name).full_id
        # Try to login with default password pattern
        agent_user = AgentMatrixUser(
            agent_name=name,
            user_id=user_id,
            display_name=f"{name.title()}Agent",
            password=f"mindroom_{name}_password",  # Default password pattern
            access_token=None,
        )

        try:
            client = await login_agent_user(homeserver, agent_user)
            existing_users[user_id] = client
            logger.info(f"Found existing mindroom user: {user_id}")
        except Exception:
            # User doesn't exist or can't login, that's fine
            pass

    return existing_users


async def audit_and_fix_room_memberships(homeserver: str | None = None) -> dict[str, list[str]]:
    """Audit all mindroom bot room memberships and fix discrepancies.

    This function:
    1. Finds all existing mindroom Matrix users
    2. Checks which rooms they're in
    3. Compares with the current configuration
    4. Removes bots from rooms they shouldn't be in
    5. Returns a report of actions taken

    Args:
        homeserver: The Matrix homeserver URL (uses default if not provided)

    Returns:
        Dictionary with keys 'removed' and 'errors' containing lists of action descriptions
    """
    homeserver = homeserver or MATRIX_HOMESERVER
    config = load_config()
    report: dict[str, list[str]] = {"removed": [], "errors": [], "checked": []}

    logger.info("Starting room membership audit...")

    # Get all existing mindroom users
    existing_users = await get_all_existing_mindroom_users(homeserver)

    if not existing_users:
        logger.info("No existing mindroom users found")
        return report

    # Build a map of which user should be in which rooms based on config
    configured_memberships = {}

    # Router should be in all configured rooms
    router_id = MatrixID.from_agent(ROUTER_AGENT_NAME, extract_server_name_from_homeserver(homeserver)).full_id
    all_rooms = set()
    for agent_config in config.agents.values():
        all_rooms.update(agent_config.rooms)
    for team_config in config.teams.values():
        all_rooms.update(team_config.rooms)
    configured_memberships[router_id] = all_rooms

    # Add agent room memberships
    server_name = extract_server_name_from_homeserver(homeserver)
    for agent_name, agent_config in config.agents.items():
        user_id = MatrixID.from_agent(agent_name, server_name).full_id
        configured_memberships[user_id] = set(agent_config.rooms)

    # Add team room memberships
    for team_name, team_config in config.teams.items():
        user_id = MatrixID.from_agent(team_name, server_name).full_id
        configured_memberships[user_id] = set(team_config.rooms)

    # Now check each existing user
    for user_id, client in existing_users.items():
        report["checked"].append(user_id)

        try:
            # Get rooms this user is currently in
            joined_rooms_response = await client.joined_rooms()
            if not isinstance(joined_rooms_response, nio.JoinedRoomsResponse):
                report["errors"].append(f"Failed to get rooms for {user_id}")
                continue

            current_rooms = set(joined_rooms_response.rooms)

            # Get rooms this user should be in (empty set if not configured)
            configured_rooms = configured_memberships.get(user_id, set())

            # Find rooms to leave (in current but not in configured)
            rooms_to_leave = current_rooms - configured_rooms

            # Leave rooms that shouldn't be in
            for room_id in rooms_to_leave:
                try:
                    leave_response = await client.room_leave(room_id)
                    if isinstance(leave_response, nio.RoomLeaveResponse):
                        msg = f"Removed {user_id} from {room_id} (not configured)"
                        logger.info(msg)
                        report["removed"].append(msg)
                    else:
                        msg = f"Failed to remove {user_id} from {room_id}: {leave_response}"
                        logger.warning(msg)
                        report["errors"].append(msg)
                except Exception as e:
                    msg = f"Error removing {user_id} from {room_id}: {e}"
                    logger.error(msg)
                    report["errors"].append(msg)

            # Note: We don't join missing rooms here - that's handled when the bot starts

        except Exception as e:
            msg = f"Error processing {user_id}: {e}"
            logger.error(msg)
            report["errors"].append(msg)

        finally:
            # Close the client
            await client.close()

    logger.info(
        f"Room membership audit complete. Checked {len(report['checked'])} users, "
        f"removed {len(report['removed'])} memberships, "
        f"encountered {len(report['errors'])} errors"
    )

    return report


async def ensure_room_memberships(homeserver: str | None = None) -> None:
    """Ensure all configured bots are in the correct rooms and not in incorrect ones.

    This is a higher-level function that both:
    1. Audits and removes incorrect memberships
    2. Could be extended to also join missing rooms if needed

    Args:
        homeserver: The Matrix homeserver URL (uses default if not provided)
    """
    report = await audit_and_fix_room_memberships(homeserver)

    if report["removed"]:
        logger.info("Room membership cleanup completed", removed_count=len(report["removed"]))

    if report["errors"]:
        logger.warning("Some room membership operations failed", error_count=len(report["errors"]))
