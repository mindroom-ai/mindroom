"""Utilities for thread analysis and agent detection."""

from typing import Any

from .matrix import extract_agent_name

# Constants
ROUTER_AGENT_NAME = "router"


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


def get_mentioned_agents(mentions: dict[str, Any]) -> list[str]:
    """Extract agent names from mentions."""
    user_ids = mentions.get("user_ids", [])
    agents = []

    for user_id in user_ids:
        agent_name = extract_agent_name(user_id)
        if agent_name:
            agents.append(agent_name)

    return agents


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


def should_agent_respond(
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    is_invited_to_thread: bool,
    room_id: str,
    configured_rooms: list[str],
    thread_history: list[dict],
    mentioned_agents: list[str] | None = None,
    team_manager: Any = None,
    thread_id: str | None = None,
) -> bool:
    """Determine if an agent should respond to a message.

    With team support: When multiple agents are in a thread or multiple
    agents are mentioned, they form a team and collaborate instead of
    responding individually.
    """
    # Avoid circular import
    from .teams import is_part_of_team_response

    # Check if agent is part of an active team response
    if is_part_of_team_response(agent_name, thread_id, team_manager):
        # Team manager will handle the response
        return False

    # For room messages (not in threads)
    if not is_thread:
        # Only respond if mentioned and have room access
        return am_i_mentioned and room_id in configured_rooms

    # Thread logic
    if am_i_mentioned:
        # Check if multiple agents are mentioned (team scenario)
        # Single agent mentioned - respond normally
        return not (mentioned_agents and len(mentioned_agents) > 1)

    # For threads, check agent participation
    agents_in_thread = get_agents_in_thread(thread_history)

    # Multiple agents in thread with no specific mention - team scenario
    if len(agents_in_thread) > 1:
        # Team will handle the response
        return False

    # Single agent continues conversation
    return [agent_name] == agents_in_thread
