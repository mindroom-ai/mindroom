"""Utilities for thread analysis and agent detection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import ROUTER_AGENT_NAME
from .matrix.identity import MatrixID, extract_agent_name
from .matrix.rooms import resolve_room_aliases

if TYPE_CHECKING:
    import nio

    from .config import Config


def check_agent_mentioned(event_source: dict, agent_id: MatrixID | None, config: Config) -> tuple[list[MatrixID], bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned).
    """
    mentions = event_source.get("content", {}).get("m.mentions", {})
    mentioned_agents = get_mentioned_agents(mentions, config)
    am_i_mentioned = agent_id in mentioned_agents
    return mentioned_agents, am_i_mentioned


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    # Thread sessions include thread ID
    return f"{room_id}:{thread_id}" if thread_id else room_id


def get_agents_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[MatrixID]:
    """Get list of unique agents that have participated in thread.

    Note: Router agent is excluded from the participant list as it's not
    a conversation participant.

    Preserves the order of first participation while preventing duplicates.
    """
    agents: list[MatrixID] = []
    seen_ids: set[str] = set()

    for msg in thread_history:
        sender: str = msg.get("sender", "")
        agent_name = extract_agent_name(sender, config)

        # Skip router agent and invalid senders
        if not agent_name or agent_name == ROUTER_AGENT_NAME:
            continue

        if sender not in seen_ids:
            try:
                matrix_id = MatrixID.parse(sender)
                agents.append(matrix_id)
                seen_ids.add(sender)
            except ValueError:
                # Skip invalid Matrix IDs
                pass

    return agents


def get_agent_matrix_ids_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[MatrixID]:
    """Get list of unique agent Matrix IDs that have participated in thread.

    Note: Router agent is excluded from the participant list as it's not
    a conversation participant.

    Preserves the order of first participation while preventing duplicates.

    Returns:
        List of MatrixID objects for agents who participated in the thread.

    """
    agent_ids = []
    seen_ids = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender, config)

        # Skip router agent and invalid senders
        if not agent_name or agent_name == ROUTER_AGENT_NAME:
            continue

        try:
            matrix_id = MatrixID.parse(sender)
            if matrix_id.full_id not in seen_ids:
                agent_ids.append(matrix_id)
                seen_ids.add(matrix_id.full_id)
        except ValueError:
            # Skip invalid Matrix IDs
            pass

    return agent_ids


def get_mentioned_agents(mentions: dict[str, Any], config: Config) -> list[MatrixID]:
    """Extract agent names from mentions."""
    user_ids = mentions.get("user_ids", [])
    agents: list[MatrixID] = []

    for user_id in user_ids:
        mid = MatrixID.parse(user_id)
        if mid.agent_name(config):
            agents.append(mid)

    return agents


def has_user_responded_after_message(
    thread_history: list[dict],
    target_event_id: str,
    user_id: MatrixID,
) -> bool:
    """Check if a user has sent any messages after a specific message in the thread.

    Args:
        thread_history: List of messages in the thread
        target_event_id: The event ID to check after
        user_id: The user ID to check for

    Returns:
        True if the user has responded after the target message

    """
    # Find the target message and check for user responses after it
    found_target = False
    for msg in thread_history:
        if msg["event_id"] == target_event_id:
            found_target = True
        elif found_target and msg["sender"] == user_id.full_id:
            return True
    return False


def get_available_agents_in_room(room: nio.MatrixRoom, config: Config) -> list[MatrixID]:
    """Get list of available agent MatrixIDs in a room.

    Note: Router agent is excluded as it's not a regular conversation participant.
    """
    agents: list[MatrixID] = []

    for member_id in room.users:
        mid = MatrixID.parse(member_id)
        agent_name = mid.agent_name(config)
        # Exclude router agent
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            agents.append(mid)

    return sorted(agents, key=lambda x: x.full_id)


def get_available_agent_matrix_ids_in_room(room: nio.MatrixRoom, config: Config) -> list[MatrixID]:
    """Get list of available agent Matrix IDs in a room.

    Note: Router agent is excluded as it's not a regular conversation participant.

    Returns:
        List of MatrixID objects for agents in the room.

    """
    agent_ids = []

    for member_id in room.users:
        agent_name = extract_agent_name(member_id, config)
        # Exclude router agent
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            try:
                matrix_id = MatrixID.parse(member_id)
                agent_ids.append(matrix_id)
            except ValueError:
                # Skip invalid Matrix IDs
                pass

    return sorted(agent_ids, key=lambda x: x.full_id)


def get_configured_agents_for_room(room_id: str, config: Config) -> list[MatrixID]:
    """Get list of agent MatrixIDs configured for a specific room.

    This returns only agents that have the room in their configuration,
    not just agents that happen to be present in the room.

    Note: Router agent is excluded as it's not a regular conversation participant.
    """
    configured_agents: list[MatrixID] = []

    # Check which agents should be in this room
    for agent_name, agent_config in config.agents.items():
        if agent_name != ROUTER_AGENT_NAME:
            resolved_rooms = resolve_room_aliases(agent_config.rooms)
            if room_id in resolved_rooms:
                configured_agents.append(config.ids[agent_name])

    return sorted(configured_agents, key=lambda x: x.full_id)


def has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]], config: Config) -> bool:
    """Check if any agents are mentioned anywhere in the thread."""
    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        if get_mentioned_agents(mentions, config):
            return True
    return False


def get_all_mentioned_agents_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[MatrixID]:
    """Get all unique agent MatrixIDs that have been mentioned anywhere in the thread.

    Preserves the order of first mention while preventing duplicates.
    """
    mentioned_agents = []
    seen_ids = set()

    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        agents = get_mentioned_agents(mentions, config)

        # Add agents in order, but only if not seen before
        for agent in agents:
            if agent.full_id not in seen_ids:
                mentioned_agents.append(agent)
                seen_ids.add(agent.full_id)

    return mentioned_agents


def is_authorized_sender(sender_id: str, config: Config, room_id: str) -> bool:
    """Check if a sender is authorized to interact with agents.

    Args:
        sender_id: Matrix ID of the message sender
        config: Application configuration
        room_id: Room ID for permission checks

    Returns:
        True if the sender is authorized, False otherwise

    """
    # Always allow mindroom_user on the current domain
    mindroom_user_id = f"@mindroom_user:{config.domain}"
    if sender_id == mindroom_user_id:
        return True

    # Check if sender is an agent or team
    agent_name = extract_agent_name(sender_id, config)
    if agent_name:
        # Agent is either in config.agents, config.teams, or is the router
        return agent_name in config.agents or agent_name in config.teams or agent_name == ROUTER_AGENT_NAME

    # Check global authorized users (they have access to all rooms)
    if sender_id in config.authorization.global_users:
        return True

    # Check room-specific permissions
    if room_id in config.authorization.room_permissions:
        return sender_id in config.authorization.room_permissions[room_id]

    # Use default access for rooms not explicitly configured
    return config.authorization.default_room_access


def should_agent_respond(
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    room: nio.MatrixRoom,
    thread_history: list[dict],
    config: Config,
    mentioned_agents: list[MatrixID] | None = None,
) -> bool:
    """Determine if an agent should respond to a message individually.

    Team formation is handled elsewhere - this just determines individual responses.

    Args:
        agent_name: Name of the agent checking if it should respond
        am_i_mentioned: Whether this specific agent is mentioned
        is_thread: Whether the message is in a thread
        room: The Matrix room object
        thread_history: History of messages in the thread
        config: Application configuration
        mentioned_agents: List of all agent MatrixIDs mentioned in the message

    """
    # Always respond if mentioned
    if am_i_mentioned:
        return True

    # Never respond if other agents are mentioned but not this one
    # (User explicitly wants a different agent)
    if mentioned_agents:
        return False

    # Non-thread messages: allow a single available agent to respond automatically
    # This applies to both DM and regular rooms. Router is excluded from availability.
    if not is_thread:
        available_agents = get_available_agents_in_room(room, config)
        return len(available_agents) == 1

    agent_matrix_id = config.ids[agent_name]

    # For threads, check if agents have already participated
    if is_thread:
        agents_in_thread = get_agents_in_thread(thread_history, config)
        if agents_in_thread:
            # Continue only if we're the single agent
            return len(agents_in_thread) == 1 and agents_in_thread[0] == agent_matrix_id

    # No agents in thread yet OR DM room without thread
    # Respond if we're the only agent available
    available_agents = get_available_agents_in_room(room, config)
    return len(available_agents) == 1
