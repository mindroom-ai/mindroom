"""Utilities for thread analysis and agent detection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import ROUTER_AGENT_NAME
from .matrix.identity import extract_agent_name

if TYPE_CHECKING:
    import nio

    from .config import Config


def check_agent_mentioned(event_source: dict, agent_name: str, config: Config) -> tuple[list[str], bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned).
    """
    mentions = event_source.get("content", {}).get("m.mentions", {})
    mentioned_agents = get_mentioned_agents(mentions, config)
    am_i_mentioned = agent_name in mentioned_agents
    return mentioned_agents, am_i_mentioned


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    # Thread sessions include thread ID
    return f"{room_id}:{thread_id}" if thread_id else room_id


def get_agents_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[str]:
    """Get list of unique agents that have participated in thread.

    Note: Router agent is excluded from the participant list as it's not
    a conversation participant.

    Preserves the order of first participation while preventing duplicates.
    """
    agents = []
    seen_agents = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender, config)
        if agent_name and agent_name != ROUTER_AGENT_NAME and agent_name not in seen_agents:
            agents.append(agent_name)
            seen_agents.add(agent_name)

    return agents


def get_mentioned_agents(mentions: dict[str, Any], config: Config) -> list[str]:
    """Extract agent names from mentions."""
    user_ids = mentions.get("user_ids", [])
    agents = []

    for user_id in user_ids:
        agent_name = extract_agent_name(user_id, config)
        if agent_name:
            agents.append(agent_name)

    return agents


def has_user_responded_after_message(thread_history: list[dict], target_event_id: str, user_id: str) -> bool:
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
        elif found_target and msg["sender"] == user_id:
            return True
    return False


def get_available_agents_in_room(room: nio.MatrixRoom, config: Config) -> list[str]:
    """Get list of available agents in a room.

    Note: Router agent is excluded as it's not a regular conversation participant.
    """
    agents = []
    room_members = list(room.users.keys()) if room.users else []

    for member_id in room_members:
        agent_name = extract_agent_name(member_id, config)
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            agents.append(agent_name)

    return sorted(agents)


def has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]], config: Config) -> bool:
    """Check if any agents are mentioned anywhere in the thread."""
    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        if get_mentioned_agents(mentions, config):
            return True
    return False


def get_all_mentioned_agents_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[str]:
    """Get all unique agents that have been mentioned anywhere in the thread.

    Preserves the order of first mention while preventing duplicates.
    """
    mentioned_agents = []
    seen_agents = set()

    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        agents = get_mentioned_agents(mentions, config)

        # Add agents in order, but only if not seen before
        for agent in agents:
            if agent not in seen_agents:
                mentioned_agents.append(agent)
                seen_agents.add(agent)

    return mentioned_agents


def should_agent_respond(  # noqa: PLR0911, C901
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    room: nio.MatrixRoom,
    is_dm_room: bool,
    configured_rooms: list[str],
    thread_history: list[dict],
    config: Config,
    mentioned_agents: list[str] | None = None,
) -> bool:
    """Determine if an agent should respond to a message individually.

    Team formation is handled elsewhere - this just determines individual responses.

    Args:
        agent_name: Name of the agent checking if it should respond
        am_i_mentioned: Whether this specific agent is mentioned
        is_thread: Whether the message is in a thread
        room: The Matrix room object
        is_dm_room: Whether this is a DM room
        configured_rooms: Rooms this agent is configured for
        thread_history: History of messages in the thread
        config: Application configuration
        mentioned_agents: List of all agents mentioned in the message

    """
    # Check if agent has access (either configured for room or in DM)
    has_room_access = room.room_id in configured_rooms or is_dm_room
    # Also check if agent is actually in the room (joined it but not configured)
    is_in_room = agent_name in [
        extract_agent_name(user_id, config) for user_id in (room.users.keys() if room.users else [])
    ]
    # has_access means either configured for room/DM or explicitly in the room
    has_access = has_room_access or is_in_room

    # For room messages (not in threads)
    if not is_thread:
        # In regular rooms, only respond if mentioned AND has access
        if not is_dm_room:
            return am_i_mentioned and has_access

        # Special case: DM room without thread started yet
        # If mentioned, respond
        if am_i_mentioned:
            return True

        # If other agents mentioned but not this one, don't respond
        if mentioned_agents and not am_i_mentioned:
            return False

        # No mentions - check how many agents are in the room
        available_agents = get_available_agents_in_room(room, config)
        # Single agent in DM should respond naturally
        # Multiple agents or unable to determine - let team formation handle it
        return len(available_agents) == 1 and agent_name in available_agents

    # If other agents are mentioned but not this one, don't respond
    # This handles the case where a user explicitly redirects to another agent
    if mentioned_agents and not am_i_mentioned:
        return False

    # Thread logic - agents respond if mentioned and have access
    if am_i_mentioned and has_access:
        return True

    # For threads, check agent participation (excluding router)
    agents_in_thread = get_agents_in_thread(thread_history, config)

    # Multiple agents in thread with no specific mention - team scenario
    if len(agents_in_thread) > 1:
        # Team will handle the response
        return False

    # Special case: Check if ONLY the router has participated (router is excluded from agents_in_thread)
    # If the router has sent messages but no other agents, agents should not take ownership
    # However, if this agent is in the room (even if not configured), it can respond
    if len(agents_in_thread) == 0 and has_access:
        # Check if router has actually spoken in the thread
        for msg in thread_history:
            sender = msg.get("sender", "")
            sender_agent = extract_agent_name(sender, config)
            if sender_agent == ROUTER_AGENT_NAME:
                # Router has spoken, only respond if agent is in room and mentioned
                return is_in_room and am_i_mentioned

        # No agents (including router) have spoken
        # Check if multiple agents could respond - if so, let router decide
        available_agents = get_available_agents_in_room(room, config)
        if len(available_agents) > 1:
            # Multiple agents available - let router decide who responds
            return False

        # Single agent with access - take ownership
        return agent_name in available_agents

    # Single agent continues conversation (only if has access)
    return [agent_name] == agents_in_thread and has_access
