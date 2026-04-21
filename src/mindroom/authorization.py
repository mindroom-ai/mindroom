"""Authorization utilities for sender and per-agent access checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import (
    MatrixID,
    extract_agent_name,
    managed_room_key_from_alias_localpart,
    room_alias_localpart,
)
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


logger = get_logger(__name__)


def _room_permission_lookup_keys(
    room_id: str,
    runtime_paths: RuntimePaths,
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
            managed_room_key = managed_room_key_from_alias_localpart(localpart, runtime_paths)
            if managed_room_key:
                keys.append(managed_room_key)
    return list(dict.fromkeys(keys))


def _lookup_managed_room_identifiers(
    room_id: str,
    runtime_paths: RuntimePaths,
) -> tuple[str | None, str | None]:
    """Return managed room key + alias from persisted Matrix state for a room ID."""
    state = MatrixState.load(runtime_paths=runtime_paths)
    for room_key, room in state.rooms.items():
        if room.room_id == room_id:
            return room_key, room.alias
    return None, None


def is_authorized_sender(
    sender_id: str,
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_alias: str | None = None,
) -> bool:
    """Check if a sender is authorized to interact with agents.

    Args:
        sender_id: Matrix ID of the message sender
        config: Application configuration
        room_id: Room ID for permission checks
        runtime_paths: Explicit runtime context for Matrix identity resolution
        room_alias: Optional canonical room alias for permission checks

    Returns:
        True if the sender is authorized, False otherwise

    """
    # Always allow configured internal user on the current domain.
    mindroom_user_id = config.get_mindroom_user_id(runtime_paths)
    if mindroom_user_id is not None and sender_id == mindroom_user_id:
        return True

    # Check if sender is an agent or team
    agent_name = extract_agent_name(sender_id, config, runtime_paths)
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
    for permission_key in _room_permission_lookup_keys(room_id, room_alias=room_alias, runtime_paths=runtime_paths):
        if permission_key in room_permissions:
            return resolved_id in room_permissions[permission_key]

    # If callers didn't provide room_alias, try persisted managed-room identifiers
    # so room key/alias permissions still work when only room_id is available.
    if room_id.startswith("!") and not all(key.startswith("!") for key in room_permissions):
        room_key, persisted_alias = _lookup_managed_room_identifiers(room_id, runtime_paths)
        for permission_key in _room_permission_lookup_keys(
            room_id,
            room_alias=persisted_alias,
            room_key=room_key,
            runtime_paths=runtime_paths,
        ):
            if permission_key in room_permissions:
                return resolved_id in room_permissions[permission_key]

    # Use default access for rooms not explicitly configured
    return config.authorization.default_room_access


def is_sender_allowed_for_agent_reply(
    sender_id: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
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
    mindroom_user_id = config.get_mindroom_user_id(runtime_paths)
    if (mindroom_user_id is not None and sender_id == mindroom_user_id) or extract_agent_name(
        sender_id,
        config,
        runtime_paths,
    ):
        return True

    resolved_sender = config.authorization.resolve_alias(sender_id)
    return any(fnmatchcase(resolved_sender, allowed_user) for allowed_user in allowed_users)


def get_effective_sender_id_for_reply_permissions(
    sender_id: str,
    event_source: Mapping[str, Any] | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the sender ID used for per-agent reply permission checks.

    Internal MindRoom senders may relay user-originated messages (voice
    transcriptions, scheduled task fires, etc.) and include the original sender
    in event content. For trusted internal senders, use that embedded sender.
    """
    known_internal_ids = {matrix_id.full_id for matrix_id in config.get_ids(runtime_paths).values()}
    mindroom_user_id = config.get_mindroom_user_id(runtime_paths)
    is_internal_mindroom_sender = (
        mindroom_user_id is not None and sender_id == mindroom_user_id
    ) or sender_id in known_internal_ids
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
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return only agents that may reply to *sender_id* per config rules."""
    result: list[MatrixID] = []
    for agent in agents:
        name = agent.agent_name(config, runtime_paths)
        if name and is_sender_allowed_for_agent_reply(sender_id, name, config, runtime_paths):
            result.append(agent)
    return result


def _available_agents_from_member_ids(
    member_ids: Iterable[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return non-router agent IDs present in one membership snapshot."""
    agents: list[MatrixID] = []
    for member_id in member_ids:
        mid = MatrixID.parse(member_id)
        agent_name = mid.agent_name(config, runtime_paths)
        if agent_name and agent_name != ROUTER_AGENT_NAME:
            agents.append(mid)
    return sorted(agents, key=lambda x: x.full_id)


def get_available_agents_in_room(
    room: nio.MatrixRoom,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Get list of available agent MatrixIDs in a room.

    Note: Router agent is excluded as it's not a regular conversation participant.
    """
    return _available_agents_from_member_ids(room.users, config, runtime_paths)


def get_available_agents_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return room agents that may reply to *sender_id*."""
    return filter_agents_by_sender_permissions(
        get_available_agents_in_room(room, config, runtime_paths),
        sender_id,
        config,
        runtime_paths,
    )


def _apply_authoritative_joined_members(
    room: nio.MatrixRoom,
    members: Sequence[nio.RoomMember],
) -> None:
    """Replace one room's cached joined-member snapshot with authoritative data."""
    members_by_user_id = {member.user_id: member for member in members}

    for user_id in tuple(room.users):
        cached_user = room.users[user_id]
        if not cached_user.invited and user_id not in members_by_user_id:
            room.remove_member(user_id)

    for member in members:
        cached_user = room.users.get(member.user_id)
        if (
            cached_user is not None
            and cached_user.display_name == member.display_name
            and cached_user.avatar_url == member.avatar_url
        ):
            continue
        if cached_user is not None:
            room.remove_member(member.user_id)
        room.add_member(member.user_id, member.display_name, member.avatar_url)

    room.members_synced = True


async def get_available_agents_for_sender_authoritative(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return sender-visible room agents, refreshing membership while the cache is unsynced."""
    cached_room_agents = get_available_agents_in_room(room, config, runtime_paths)
    cached_visible_agents = filter_agents_by_sender_permissions(
        cached_room_agents,
        sender_id,
        config,
        runtime_paths,
    )
    if room.members_synced:
        return cached_visible_agents

    response = await client.joined_members(room.room_id)
    if not isinstance(response, nio.JoinedMembersResponse):
        logger.warning(
            "authoritative_room_membership_fetch_failed",
            room_id=room.room_id,
            sender_id=sender_id,
            error=str(response),
        )
        return cached_visible_agents

    _apply_authoritative_joined_members(room, response.members)
    refreshed_room_agents = _available_agents_from_member_ids(
        (member.user_id for member in response.members),
        config,
        runtime_paths,
    )
    refreshed_agents = filter_agents_by_sender_permissions(
        refreshed_room_agents,
        sender_id,
        config,
        runtime_paths,
    )
    logger.info(
        "authoritative_room_membership_refreshed",
        room_id=room.room_id,
        sender_id=sender_id,
        cached_agent_count=len(cached_room_agents),
        refreshed_agent_count=len(refreshed_agents),
    )
    return refreshed_agents
