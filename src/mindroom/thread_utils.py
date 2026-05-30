"""Utilities for thread analysis and agent detection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from mindroom import authorization
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.mentions import resolve_mentioned_user_ids_from_text
from mindroom.matrix.visible_body import visible_content_from_content

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID


# Matches <a href="https://matrix.to/#/@user:domain">...</a> pills used by bridges.
# Accepts both single and double quotes (mautrix bridges use single quotes).
# Requires @localpart:domain format to avoid feeding malformed IDs to MatrixID.parse.
_MATRIX_PILL_RE = re.compile(r"""href=["']https://matrix\.to/#/(@[^"':]+:[^"']+)["']""")


def _extract_mentioned_user_ids(
    content: dict[str, object],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Extract mentioned user IDs from message content.

    Checks ``m.mentions.user_ids`` first. When that field is absent or empty,
    falls back to Matrix HTML pills and finally raw visible-body mention tokens.
    """
    mentions = content.get("m.mentions")
    user_ids = cast("dict[str, object]", mentions).get("user_ids") if isinstance(mentions, dict) else None
    if isinstance(user_ids, list) and user_ids:
        return [user_id for user_id in user_ids if isinstance(user_id, str)]

    formatted_body = content.get("formatted_body")
    if isinstance(formatted_body, str):
        pill_user_ids = _MATRIX_PILL_RE.findall(formatted_body)
        if pill_user_ids:
            return pill_user_ids

    body = content.get("body")
    if isinstance(body, str):
        return resolve_mentioned_user_ids_from_text(body, config, runtime_paths)
    return []


def _is_bot_or_agent(sender: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return True when *sender* is a MindRoom agent **or** listed in ``bot_accounts``."""
    registry = entity_identity_registry(config, runtime_paths)
    return registry.current_entity_name_for_user_id(sender) is not None or sender in config.bot_accounts


def is_router_only_agent_mention(
    mentioned_agents: Sequence[MatrixID],
    *,
    has_non_agent_mentions: bool,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether the message only targeted the router managed account."""
    if has_non_agent_mentions or not mentioned_agents:
        return False

    registry = entity_identity_registry(config, runtime_paths)
    mentioned_agent_names = {registry.current_entity_name_for_user_id(agent.full_id) for agent in mentioned_agents}
    return mentioned_agent_names == {ROUTER_AGENT_NAME}


def check_agent_mentioned(
    event_source: dict,
    agent_id: MatrixID | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[list[MatrixID], bool, bool]:
    """Check if an agent is mentioned in a message.

    Returns (mentioned_agents, am_i_mentioned, has_non_agent_mentions).
    ``has_non_agent_mentions`` is True when the message explicitly tags a
    user who is *not* a configured agent and not in ``config.bot_accounts``
    (i.e. a real human user).
    """
    raw_content = event_source.get("content", {})
    content = visible_content_from_content(raw_content) if isinstance(raw_content, dict) else {}
    all_mentioned_ids = _extract_mentioned_user_ids(content, config, runtime_paths)
    mentioned_agents = _agents_from_user_ids(all_mentioned_ids, config, runtime_paths)
    am_i_mentioned = agent_id in mentioned_agents
    has_non_agent_mentions = any(not _is_bot_or_agent(uid, config, runtime_paths) for uid in all_mentioned_ids)

    return mentioned_agents, am_i_mentioned, has_non_agent_mentions


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    # Thread sessions include thread ID
    return f"{room_id}:{thread_id}" if thread_id else room_id


def parse_session_id(session_id: str) -> tuple[str, str | None]:
    """Parse the canonical persisted room/thread session ID."""
    room_id, marker, thread_suffix = session_id.rpartition(":$")
    return (room_id, f"${thread_suffix}") if marker else (session_id, None)


def get_agents_in_thread(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Get list of unique agents that have participated in thread.

    Note: Router agent is excluded from the participant list as it's not
    a conversation participant.

    Preserves the order of first participation while preventing duplicates.
    """
    agents: list[MatrixID] = []
    seen_ids: set[str] = set()
    registry = entity_identity_registry(config, runtime_paths)

    for msg in thread_history:
        sender = msg.sender
        agent_name = registry.current_entity_name_for_user_id(sender, include_router=False)

        # Skip router agent and invalid senders
        if agent_name is None:
            continue

        if sender not in seen_ids:
            agents.append(registry.current_id(agent_name))
            seen_ids.add(sender)

    return agents


def _agents_from_user_ids(
    user_ids: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return agent MatrixIDs from a list of raw Matrix user ID strings."""
    registry = entity_identity_registry(config, runtime_paths)
    agents: list[MatrixID] = []
    for user_id in user_ids:
        agent_name = registry.current_entity_name_for_user_id(user_id)
        if agent_name is not None:
            agents.append(registry.current_id(agent_name))
    return agents


def has_multiple_non_agent_users_in_thread(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return True when more than one non-agent user has posted in the thread.

    Senders that are MindRoom agents or listed in ``config.bot_accounts`` are
    excluded from the count.
    """
    non_agent_senders: set[str] = set()
    for msg in thread_history:
        sender = msg.sender
        if sender and not _is_bot_or_agent(sender, config, runtime_paths):
            non_agent_senders.add(sender)
            if len(non_agent_senders) > 1:
                return True
    return False


def thread_requires_explicit_agent_targeting(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    available_responders_in_room: Sequence[MatrixID] | None = None,
) -> bool:
    """Return whether a thread already has visible ownership or multiple human participants."""
    sender_visible_responders = authorization.filter_responders_by_sender_permissions(
        get_agents_in_thread(thread_history, config, runtime_paths),
        sender_id,
        config,
        runtime_paths,
    )
    if available_responders_in_room is not None:
        available_responder_ids = {responder.full_id for responder in available_responders_in_room}
        sender_visible_responders = [
            responder for responder in sender_visible_responders if responder.full_id in available_responder_ids
        ]
    if sender_visible_responders:
        return True
    return has_multiple_non_agent_users_in_thread(thread_history, config, runtime_paths)


def get_all_mentioned_agents_in_thread(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Get all unique agent MatrixIDs that have been mentioned anywhere in the thread.

    Preserves the order of first mention while preventing duplicates.
    """
    mentioned_agents = []
    seen_ids: set[str] = set()

    for msg in thread_history:
        content = msg.content
        user_ids = _extract_mentioned_user_ids(content, config, runtime_paths)
        agents = _agents_from_user_ids(user_ids, config, runtime_paths)

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
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    mentioned_agents: list[MatrixID] | None = None,
    has_non_agent_mentions: bool = False,
    *,
    sender_id: str,
    available_responders_in_room: list[MatrixID] | None = None,
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
        runtime_paths: Explicit runtime context for permissions and mention resolution
        mentioned_agents: List of all agent MatrixIDs mentioned in the message
        has_non_agent_mentions: True when the message explicitly tags a non-agent user
        sender_id: Sender Matrix ID used for per-agent reply permissions
        available_responders_in_room: Optional precomputed sender-visible responders for the room

    """
    if not authorization.is_sender_allowed_for_agent_reply(sender_id, agent_name, config, runtime_paths):
        return False

    available_responders = available_responders_in_room
    if available_responders is None:
        available_responders = authorization.responder_candidate_entities_from_cached_room(
            room,
            sender_id,
            config,
            runtime_paths,
        )
    agent_matrix_id = entity_identity_registry(config, runtime_paths).current_id(agent_name)
    available_responder_ids = {responder.full_id for responder in available_responders}
    if agent_matrix_id.full_id not in available_responder_ids:
        return False

    # Always respond if mentioned
    if am_i_mentioned:
        return True

    # Never respond if anyone else is explicitly mentioned (agent or not)
    if mentioned_agents or has_non_agent_mentions:
        return False

    # Non-thread messages: auto-respond if we're the only visible responder in the room.
    if not is_thread:
        return len(available_responders) == 1 and available_responders[0] == agent_matrix_id

    # In threads with multiple human participants, always require explicit mention.
    if has_multiple_non_agent_users_in_thread(thread_history, config, runtime_paths):
        return False

    # For threads, continue only if we're the single participating agent
    # that may reply to this sender within this room's responder boundary.
    agents_in_thread = get_agents_in_thread(thread_history, config, runtime_paths)
    agents_in_thread = authorization.filter_responders_by_sender_permissions(
        agents_in_thread,
        sender_id,
        config,
        runtime_paths,
    )
    agents_in_thread = [agent for agent in agents_in_thread if agent.full_id in available_responder_ids]
    if agents_in_thread:
        return len(agents_in_thread) == 1 and agents_in_thread[0] == agent_matrix_id

    # No agents in thread yet: respond if we're the only visible responder.
    return len(available_responders) == 1 and available_responders[0] == agent_matrix_id
