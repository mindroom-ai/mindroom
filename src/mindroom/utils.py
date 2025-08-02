"""Common utility functions used across the codebase."""

from typing import NamedTuple


class ResponseDecision(NamedTuple):
    """Decision about whether an agent should respond to a message."""

    should_respond: bool
    use_router: bool


def extract_domain_from_user_id(user_id: str) -> str:
    """Extract domain from a Matrix user ID like "@user:example.com"."""
    if ":" in user_id:
        return user_id.split(":", 1)[1]
    return "localhost"


def extract_username_from_user_id(user_id: str) -> str:
    """Extract username from a Matrix user ID like "@mindroom_calculator:example.com"."""
    if user_id.startswith("@"):
        username = user_id[1:]  # Remove @
        if ":" in username:
            return username.split(":", 1)[0]
        return username
    return user_id


def extract_server_name_from_homeserver(homeserver: str) -> str:
    """Extract server name from a homeserver URL like "http://localhost:8008"."""
    # Remove protocol
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver

    # Remove port if present
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part


def construct_agent_user_id(agent_name: str, domain: str) -> str:
    """Construct a Matrix user ID for an agent like "@mindroom_calculator:localhost"."""
    return f"@mindroom_{agent_name}:{domain}"


def extract_thread_info(event_source: dict) -> tuple[bool, str | None]:
    """Extract thread information from a Matrix event.

    Returns (is_thread, thread_id).
    """
    relates_to = event_source.get("content", {}).get("m.relates_to", {})
    is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
    thread_id = relates_to.get("event_id") if is_thread else None
    return is_thread, thread_id


def check_agent_mentioned(event_source: dict, agent_name: str) -> tuple[list[str], bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned).
    """
    from .thread_utils import get_mentioned_agents

    mentions = event_source.get("content", {}).get("m.mentions", {})
    mentioned_agents = get_mentioned_agents(mentions)
    am_i_mentioned = agent_name in mentioned_agents
    return mentioned_agents, am_i_mentioned


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    return f"{room_id}:{thread_id}" if thread_id else room_id


async def has_room_access(room_id: str, agent_name: str, configured_rooms: list[str]) -> bool:
    """Check if an agent has access to a room."""
    return room_id in configured_rooms


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
    from .thread_utils import get_agents_in_thread, has_any_agent_mentions_in_thread

    should_respond = False
    use_router = False

    # Agents ONLY respond in threads, never in main rooms
    if not is_thread:
        return ResponseDecision(False, False)

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
