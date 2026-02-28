"""Utilities for thread analysis and agent detection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from .constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from .matrix.identity import MatrixID, extract_agent_name, managed_room_key_from_alias_localpart, room_alias_localpart
from .matrix.rooms import resolve_room_aliases
from .matrix.state import MatrixState

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from .config.main import Config

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


def has_any_agent_mentions_in_thread(thread_history: list[dict[str, Any]], config: Config) -> bool:
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


def _room_permission_lookup_keys(
    room_id: str,
    *,
    room_alias: str | None = None,
    room_key: str | None = None,
) -> list[str]:
    """Build room identifiers that can be used as authorization map keys."""
    keys = [room_id]
    if room_key:
        keys.append(room_key)
    if room_alias:
        keys.append(room_alias)
        localpart = room_alias_localpart(room_alias)
        if localpart:
            keys.append(localpart)
            managed_room_key = managed_room_key_from_alias_localpart(localpart)
            if managed_room_key:
                keys.append(managed_room_key)
    return list(dict.fromkeys(keys))


def _lookup_managed_room_identifiers(room_id: str) -> tuple[str | None, str | None]:
    """Return managed room key + alias from persisted Matrix state for a room ID."""
    state = MatrixState.load()
    for room_key, room in state.rooms.items():
        if room.room_id == room_id:
            return room_key, room.alias
    return None, None


def is_authorized_sender(
    sender_id: str,
    config: Config,
    room_id: str,
    *,
    room_alias: str | None = None,
) -> bool:
    """Check if a sender is authorized to interact with agents.

    Args:
        sender_id: Matrix ID of the message sender
        config: Application configuration
        room_id: Room ID for permission checks
        room_alias: Optional canonical room alias for permission checks

    Returns:
        True if the sender is authorized, False otherwise

    """
    # Always allow configured internal user on the current domain.
    if sender_id == config.get_mindroom_user_id():
        return True

    # Check if sender is an agent or team
    agent_name = extract_agent_name(sender_id, config)
    if agent_name:
        # Agent is either in config.agents, config.teams, or is the router
        return agent_name in config.agents or agent_name in config.teams or agent_name == ROUTER_AGENT_NAME

    # Resolve bridge aliases to canonical user ID before permission checks.
    resolved_id = config.authorization.resolve_alias(sender_id)

    # Check global authorized users (they have access to all rooms)
    if resolved_id in config.authorization.global_users:
        return True

    room_permissions = config.authorization.room_permissions

    # Check room-specific permissions by direct room identifiers first.
    for permission_key in _room_permission_lookup_keys(room_id, room_alias=room_alias):
        if permission_key in room_permissions:
            return resolved_id in room_permissions[permission_key]

    # If callers didn't provide room_alias, try persisted managed-room identifiers
    # so room key/alias permissions still work when only room_id is available.
    if room_id.startswith("!") and not all(key.startswith("!") for key in room_permissions):
        room_key, persisted_alias = _lookup_managed_room_identifiers(room_id)
        for permission_key in _room_permission_lookup_keys(room_id, room_alias=persisted_alias, room_key=room_key):
            if permission_key in room_permissions:
                return resolved_id in room_permissions[permission_key]

    # Use default access for rooms not explicitly configured
    return config.authorization.default_room_access


def is_sender_allowed_for_agent_reply(sender_id: str, agent_name: str, config: Config) -> bool:
    """Check whether *agent_name* is allowed to reply to *sender_id*.

    Internal MindRoom identities (agents/teams/router and internal user) bypass
    this allowlist because they are system participants, not end users.
    """
    agent_reply_permissions = config.authorization.agent_reply_permissions
    allowed_users = agent_reply_permissions.get(agent_name)
    if allowed_users is None:
        allowed_users = agent_reply_permissions.get("*")
    if allowed_users is None:
        return True
    if "*" in allowed_users:
        return True

    # Internal MindRoom participants are not restricted by per-user reply lists.
    # Bridge bot accounts are intentionally not exempt.
    if sender_id == config.get_mindroom_user_id() or extract_agent_name(sender_id, config):
        return True

    resolved_sender = config.authorization.resolve_alias(sender_id)
    return any(fnmatchcase(resolved_sender, allowed_user) for allowed_user in allowed_users)


def get_effective_sender_id_for_reply_permissions(
    sender_id: str,
    event_source: Mapping[str, Any] | None,
    config: Config,
) -> str:
    """Return the sender ID used for per-agent reply permission checks.

    Internal MindRoom senders may relay user-originated messages (voice
    transcriptions, scheduled task fires, etc.) and include the original sender
    in event content. For trusted internal senders, use that embedded sender.
    """
    known_internal_ids = {matrix_id.full_id for matrix_id in config.ids.values()}
    mindroom_user_id = config.get_mindroom_user_id()
    is_internal_mindroom_sender = sender_id == mindroom_user_id or sender_id in known_internal_ids
    if not is_internal_mindroom_sender:
        return sender_id
    if not event_source:
        return sender_id

    content = event_source.get("content")
    if not isinstance(content, Mapping):
        return sender_id

    original_sender = content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        return original_sender
    return sender_id


def filter_agents_by_sender_permissions(
    agents: Sequence[MatrixID],
    sender_id: str,
    config: Config,
) -> list[MatrixID]:
    """Return only agents that may reply to *sender_id* per config rules."""
    result: list[MatrixID] = []
    for agent in agents:
        name = agent.agent_name(config)
        if name and is_sender_allowed_for_agent_reply(sender_id, name, config):
            result.append(agent)
    return result


def get_available_agents_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
) -> list[MatrixID]:
    """Return room agents that may reply to *sender_id*."""
    return filter_agents_by_sender_permissions(get_available_agents_in_room(room, config), sender_id, config)


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
    if not is_sender_allowed_for_agent_reply(sender_id, agent_name, config):
        return False

    # Always respond if mentioned
    if am_i_mentioned:
        return True

    # Never respond if anyone else is explicitly mentioned (agent or not)
    if mentioned_agents or has_non_agent_mentions:
        return False

    available_agents = get_available_agents_for_sender(room, sender_id, config)
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
    agents_in_thread = filter_agents_by_sender_permissions(agents_in_thread, sender_id, config)
    if agents_in_thread:
        return len(agents_in_thread) == 1 and agents_in_thread[0] == agent_matrix_id

    # No agents in thread yet — respond if we're the only visible agent.
    return len(available_agents) == 1 and available_agents[0] == agent_matrix_id
