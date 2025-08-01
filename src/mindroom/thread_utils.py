"""Utilities for thread analysis and agent detection."""

from functools import lru_cache
from typing import Any

from .agent_loader import load_config


@lru_cache(maxsize=128)
def get_known_agent_names() -> set[str]:
    """Get set of all configured agent names.

    Returns:
        Set of agent names from configuration
    """
    config = load_config()
    return set(config.agents.keys())


def extract_agent_name(sender_id: str) -> str | None:
    """Extract agent name from sender ID if it's a known agent.

    Args:
        sender_id: Matrix user ID like @mindroom_calculator:localhost

    Returns:
        Agent name (e.g., 'calculator') or None if not an agent
    """
    if not sender_id.startswith("@mindroom_"):
        return None

    # Extract username part
    username = sender_id.split(":")[0][1:]  # Remove @ and domain

    # Skip regular users
    if username.startswith("mindroom_user"):
        return None

    # Extract potential agent name after mindroom_
    agent_name = username.replace("mindroom_", "")

    # Check if this is actually a configured agent
    if agent_name in get_known_agent_names():
        return agent_name

    return None


def get_agents_in_thread(thread_history: list[dict[str, Any]]) -> list[str]:
    """Get list of unique agents that have participated in thread.

    Args:
        thread_history: List of messages in thread

    Returns:
        List of agent names (not including regular users)
    """
    agents = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender)
        if agent_name:
            agents.add(agent_name)

    return sorted(list(agents))


def get_mentioned_agents(mentions: dict[str, Any]) -> list[str]:
    """Extract agent names from mentions.

    Args:
        mentions: The m.mentions object from message content

    Returns:
        List of mentioned agent names
    """
    user_ids = mentions.get("user_ids", [])
    agents = []

    for user_id in user_ids:
        agent_name = extract_agent_name(user_id)
        if agent_name:
            agents.append(agent_name)

    return agents


def get_available_agents_in_room(room: Any) -> list[str]:
    """Get list of available agents in a room.

    Args:
        room: MatrixRoom object

    Returns:
        List of agent names available in the room
    """
    agents = []
    room_members = list(room.users.keys()) if room.users else []

    for member_id in room_members:
        agent_name = extract_agent_name(member_id)
        if agent_name:
            agents.append(agent_name)

    return sorted(agents)


def has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]]) -> bool:
    """Check if any agents are mentioned anywhere in the thread.

    Args:
        thread_history: List of messages in thread

    Returns:
        True if any agent is mentioned in any message
    """
    for msg in thread_history:
        content = msg.get("content", {})
        mentions = content.get("m.mentions", {})
        if get_mentioned_agents(mentions):
            return True
    return False
