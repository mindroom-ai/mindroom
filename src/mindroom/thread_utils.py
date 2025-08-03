"""Utilities for thread analysis and agent detection."""

from typing import Any, NamedTuple

from .matrix import extract_agent_name


class ResponseDecision(NamedTuple):
    """Decision about whether an agent should respond to a message."""

    should_respond: bool
    use_router: bool


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
    """Get list of unique agents that have participated in thread."""
    agents = set()

    for msg in thread_history:
        sender = msg.get("sender", "")
        agent_name = extract_agent_name(sender)
        if agent_name:
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
    """Get list of available agents in a room."""
    agents = []
    room_members = list(room.users.keys()) if room.users else []

    for member_id in room_members:
        agent_name = extract_agent_name(member_id)
        if agent_name:
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
) -> ResponseDecision:
    """Determine if an agent should respond to a message.

    Returns ResponseDecision with (should_respond, use_router).
    """
    should_respond = False
    use_router = False

    # For room messages (not in threads), use router to determine who responds
    if not is_thread:
        # Only agents with room access can use the router
        if room_id in configured_rooms:
            if am_i_mentioned:
                # Respond directly if mentioned
                should_respond = True
            else:
                # Use router to pick an agent
                use_router = True
        return ResponseDecision(should_respond, use_router)

    # Thread logic
    if am_i_mentioned:
        # Respond if explicitly mentioned in a thread
        should_respond = True
    else:
        # For threads, check if there's a single agent that should continue
        agents_in_thread = get_agents_in_thread(thread_history)

        # If I'm the only agent in the thread, I should continue responding
        if len(agents_in_thread) == 1 and agent_name in agents_in_thread:
            should_respond = True
        # Standard logic for all agents (native or invited)
        elif room_id in configured_rooms or is_invited_to_thread:
            if has_any_agent_mentions_in_thread(thread_history):
                # Someone is mentioned - only mentioned agents respond
                pass
            elif not agents_in_thread:
                # No agents yet - use router to pick first responder
                use_router = True
            else:
                # Multiple agents - nobody responds
                pass

    return ResponseDecision(should_respond, use_router)


def should_route_to_agent(agent_name: str, available_agents: list[str]) -> bool:
    """Determine if this agent should handle routing.

    Only one agent should handle routing to avoid duplicates.
    We use the first agent alphabetically as a deterministic choice.
    """
    if not available_agents:
        return False
    return available_agents[0] == agent_name
