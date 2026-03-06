"""Utilities for thread analysis and agent detection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from mindroom import authorization
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.rooms import resolve_room_aliases

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config

# Matches <a href="https://matrix.to/#/@user:domain">...</a> pills used by bridges.
# Accepts both single and double quotes (mautrix bridges use single quotes).
# Requires @localpart:domain format to avoid feeding malformed IDs to MatrixID.parse.
_MATRIX_PILL_RE = re.compile(r"""href=["']https://matrix\.to/#/(@[^"':]+:[^"']+)["']""")


def _extract_mentioned_user_ids(content: dict[str, Any]) -> list[str]:
    """Extract mentioned user IDs from message content.

    Checks ``m.mentions.user_ids`` first.  When that field is absent or empty
    (common with bridges like mautrix-telegram), falls back to parsing Matrix
    HTML pills (``<a href="https://matrix.to/#/@user:domain">``) from
    ``formatted_body``.
    """
    mentions = content.get("m.mentions", {})
    user_ids: list[str] = mentions.get("user_ids", [])
    if user_ids:
        return user_ids

    # Fallback: parse formatted_body for HTML pills
    formatted_body = content.get("formatted_body", "")
    if formatted_body:
        return _MATRIX_PILL_RE.findall(formatted_body)
    return []


def _is_bot_or_agent(sender: str, config: Config) -> bool:
    """Return True when *sender* is a MindRoom agent **or** listed in ``bot_accounts``."""
    return bool(extract_agent_name(sender, config)) or sender in config.bot_accounts


def check_agent_mentioned(
    event_source: dict,
    agent_id: MatrixID | None,
    config: Config,
) -> tuple[list[MatrixID], bool, bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned, has_non_agent_mentions).
    ``has_non_agent_mentions`` is True when the message explicitly tags a
    user who is *not* a configured agent and not in ``config.bot_accounts``
    (i.e. a real human user).
    """
    content = event_source.get("content", {})
    all_mentioned_ids = _extract_mentioned_user_ids(content)
    mentioned_agents = _agents_from_user_ids(all_mentioned_ids, config)
    am_i_mentioned = agent_id in mentioned_agents
    has_non_agent_mentions = any(not _is_bot_or_agent(uid, config) for uid in all_mentioned_ids)

    return mentioned_agents, am_i_mentioned, has_non_agent_mentions


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


def _agents_from_user_ids(user_ids: list[str], config: Config) -> list[MatrixID]:
    """Return agent MatrixIDs from a list of raw Matrix user ID strings."""
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


def has_multiple_non_agent_users_in_thread(thread_history: list[dict[str, Any]], config: Config) -> bool:
    """Return True when more than one non-agent user has posted in the thread.

    Senders that are MindRoom agents or listed in ``config.bot_accounts`` are
    excluded from the count.
    """
    non_agent_senders: set[str] = set()
    for msg in thread_history:
        sender: str = msg.get("sender", "")
        if sender and not _is_bot_or_agent(sender, config):
            non_agent_senders.add(sender)
            if len(non_agent_senders) > 1:
                return True
    return False


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


def _has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]], config: Config) -> bool:
    """Check if any agents are mentioned anywhere in the thread."""
    for msg in thread_history:
        content = msg.get("content", {})
        user_ids = _extract_mentioned_user_ids(content)
        if _agents_from_user_ids(user_ids, config):
            return True
    return False


def get_all_mentioned_agents_in_thread(thread_history: list[dict[str, Any]], config: Config) -> list[MatrixID]:
    """Get all unique agent MatrixIDs that have been mentioned anywhere in the thread.

    Preserves the order of first mention while preventing duplicates.
    """
    mentioned_agents = []
    seen_ids: set[str] = set()

    for msg in thread_history:
        content = msg.get("content", {})
        user_ids = _extract_mentioned_user_ids(content)
        agents = _agents_from_user_ids(user_ids, config)

        for agent in agents:
            if agent.full_id not in seen_ids:
                mentioned_agents.append(agent)
                seen_ids.add(agent.full_id)

    return mentioned_agents


def should_agent_respond(  # noqa: PLR0911
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    room: nio.MatrixRoom,
    thread_history: list[dict],
    config: Config,
    mentioned_agents: list[MatrixID] | None = None,
    has_non_agent_mentions: bool = False,
    *,
    sender_id: str,
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
        has_non_agent_mentions: True when the message explicitly tags a non-agent user
        sender_id: Sender Matrix ID used for per-agent reply permissions

    """
    if not authorization.is_sender_allowed_for_agent_reply(sender_id, agent_name, config):
        return False

    # Always respond if mentioned
    if am_i_mentioned:
        return True

    # Never respond if anyone else is explicitly mentioned (agent or not)
    if mentioned_agents or has_non_agent_mentions:
        return False

    available_agents = authorization.get_available_agents_for_sender(room, sender_id, config)
    agent_matrix_id = config.ids[agent_name]

    # Non-thread messages: auto-respond if we're the only visible agent in the room.
    if not is_thread:
        return len(available_agents) == 1 and available_agents[0] == agent_matrix_id

    # In threads with multiple human participants, always require explicit mention.
    if has_multiple_non_agent_users_in_thread(thread_history, config):
        return False

    # For threads, continue only if we're the single participating agent
    # that may reply to this sender.
    agents_in_thread = get_agents_in_thread(thread_history, config)
    agents_in_thread = authorization.filter_agents_by_sender_permissions(agents_in_thread, sender_id, config)
    if agents_in_thread:
        return len(agents_in_thread) == 1 and agents_in_thread[0] == agent_matrix_id

    # No agents in thread yet — respond if we're the only visible agent.
    return len(available_agents) == 1 and available_agents[0] == agent_matrix_id
