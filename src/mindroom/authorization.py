"""Authorization utilities for sender and per-agent access checks."""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.matrix.identity import (
    MatrixID,
    extract_agent_name,
    managed_room_key_from_alias_localpart,
    room_alias_localpart,
)
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.config.main import Config


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


def get_available_agents_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
) -> list[MatrixID]:
    """Return room agents that may reply to *sender_id*."""
    return filter_agents_by_sender_permissions(
        get_available_agents_in_room(room, config),
        sender_id,
        config,
    )
