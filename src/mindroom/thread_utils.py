"""Utilities for thread analysis and agent detection."""

from typing import Any

from .agent_config import ROUTER_AGENT_NAME
from .matrix import extract_agent_name


def check_agent_mentioned(event_source: dict, agent_name: str) -> tuple[list[str], bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned).
    """
    mentions = event_source.get("content", {}).get("m.mentions", {})
    mentioned_agents = get_mentioned_agents(mentions)
    am_i_mentioned = agent_name in mentioned_agents
    return mentioned_agents, am_i_mentioned


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    return f"{room_id}:{thread_id}" if thread_id else room_id


def get_agents_in_thread(thread_history: list[dict[str, Any]]) -> list[str]:
    """Get list of unique agents that have participated in thread.

    Note: Router agent is excluded from the participant list as it's not
    a conversation participant.
    """
    agents = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender)
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            agents.add(agent_name)

    return sorted(list(agents))


def get_all_agents_in_thread_including_router(thread_history: list[dict[str, Any]]) -> list[str]:
    """Get list of ALL agents that have sent messages in thread, including RouterAgent.

    This is used to detect when ANY agent (including router) has sent a message,
    which is important for preventing cascade responses.
    """
    agents = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender)
        if agent_name:  # Include ALL agents, even RouterAgent
            agents.add(agent_name)

    return sorted(list(agents))


def get_mentioned_agents(mentions: dict[str, Any]) -> list[str]:
    """Extract agent names from mentions."""
    user_ids = mentions.get("user_ids", [])
    agents = []

    for user_id in user_ids:
        agent_name = extract_agent_name(user_id)
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


def get_available_agents_in_room(room: Any) -> list[str]:
    """Get list of available agents in a room.

    Note: Router agent is excluded as it's not a regular conversation participant.
    """
    agents = []
    room_members = list(room.users.keys()) if room.users else []

    for member_id in room_members:
        agent_name = extract_agent_name(member_id)
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            agents.append(agent_name)

    return sorted(agents)


def has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]]) -> bool:
    """Check if any agents are mentioned anywhere in the thread."""
    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        if get_mentioned_agents(mentions):
            return True
    return False


def get_all_mentioned_agents_in_thread(thread_history: list[dict[str, Any]]) -> list[str]:
    """Get all unique agents that have been mentioned anywhere in the thread."""
    mentioned_agents = set()

    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        agents = get_mentioned_agents(mentions)
        mentioned_agents.update(agents)

    return sorted(list(mentioned_agents))


def should_agent_respond(
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    room_id: str,
    configured_rooms: list[str],
    thread_history: list[dict],
    current_sender: str | None = None,
    current_mentions: list[str] | None = None,
) -> bool:
    """Determine if an agent should respond to a message individually.

    Team formation is handled elsewhere - this just determines individual responses.

    Args:
        agent_name: Name of the agent considering response
        am_i_mentioned: Whether this agent is mentioned
        is_thread: Whether this is a thread message
        room_id: The room ID
        configured_rooms: Rooms the agent has access to
        thread_history: Previous messages in thread
        current_sender: Sender of the current message being processed
        current_mentions: Agents mentioned in the current message
    """

    # For room messages (not in threads)
    if not is_thread:
        # Only respond if mentioned and have room access
        return am_i_mentioned and room_id in configured_rooms

    # Thread logic
    if am_i_mentioned:
        return True

    # Check if the CURRENT message is from RouterAgent without mentions
    # This handles the case where router sends an error and we're deciding whether to respond
    if current_sender:
        sender_agent = extract_agent_name(current_sender)
        if sender_agent == ROUTER_AGENT_NAME and not current_mentions:
            # Router sent a message without mentioning anyone - nobody should respond
            return False

    # Also check if the last message in history is from RouterAgent without mentions
    # (for cases where we don't have current_sender info)
    if thread_history:
        last_msg = thread_history[-1]
        sender = last_msg.get("sender", "")
        agent_name_sender = extract_agent_name(sender)
        if agent_name_sender == ROUTER_AGENT_NAME:
            # Check if router mentioned anyone
            content = last_msg.get("content", {})
            mentions = content.get("m.mentions", {})
            mentioned_agents = get_mentioned_agents(mentions)
            if not mentioned_agents:
                # Router sent a message without mentioning anyone - nobody should respond
                return False

    # For threads, check agent participation
    agents_in_thread = get_agents_in_thread(thread_history)

    # Multiple agents in thread with no specific mention - team scenario
    if len(agents_in_thread) > 1:
        # Team will handle the response
        return False

    # Single agent continues conversation
    return [agent_name] == agents_in_thread
