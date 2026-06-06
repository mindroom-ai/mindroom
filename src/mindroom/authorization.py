"""Authorization utilities for sender and per-agent access checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.dispatch_source import source_kind_allows_trusted_original_sender, source_kind_from_content
from mindroom.entity_resolution import (
    configured_routable_entity_ids_for_room,
    current_internal_sender_ids,
    entity_identity_registry,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix_identifiers import managed_room_key_from_alias_localpart, room_alias_localpart

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID


logger = get_logger(__name__)


def _room_alias_permission_lookup_keys(room_alias: str, runtime_paths: RuntimePaths) -> list[str]:
    """Build permission keys derived from one trusted Matrix room alias."""
    keys = [room_alias]
    localpart = room_alias_localpart(room_alias)
    if localpart:
        keys.append(localpart)
        managed_room_key = managed_room_key_from_alias_localpart(localpart, runtime_paths)
        if managed_room_key:
            keys.append(managed_room_key)
    return keys


def _room_permission_lookup_keys(
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_alias: str | None = None,
    room_key: str | None = None,
) -> list[str]:
    """Build room identifiers that can be used as authorization map keys."""
    keys = [room_id]
    if room_id.startswith("#"):
        keys.extend(_room_alias_permission_lookup_keys(room_id, runtime_paths))
    if room_key:
        keys.append(room_key)
    if room_alias:
        keys.extend(_room_alias_permission_lookup_keys(room_alias, runtime_paths))
    return list(dict.fromkeys(keys))


def _lookup_managed_room_identifiers(
    room_id: str,
    runtime_paths: RuntimePaths,
) -> tuple[str | None, str | None]:
    """Return managed room key + alias from persisted Matrix state for a room ID."""
    state = matrix_state_for_runtime(runtime_paths)
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
        room_alias: Accepted for call-site compatibility but intentionally
            ignored. Caller-supplied canonical aliases are mutable Matrix room
            state and are never used for permission-key lookup.

    Returns:
        True if the sender is authorized, False otherwise

    """
    # Always allow active internal identities owned by this runtime.
    if sender_id in current_internal_sender_ids(config, runtime_paths):
        return True

    # Resolve bridge aliases to canonical user ID before permission checks.
    resolved_id = config.authorization.resolve_alias(sender_id)

    # Check global authorized users (they have access to all rooms)
    if resolved_id in config.authorization.global_users:
        return True

    room_permissions = config.authorization.room_permissions
    _ = room_alias
    # Check room-specific permissions by direct room identifiers first. A room
    # ID is stable; a direct alias target is an explicit caller target rather
    # than room state content.
    for permission_key in _room_permission_lookup_keys(room_id, runtime_paths=runtime_paths):
        if permission_key in room_permissions:
            return resolved_id in room_permissions[permission_key]

    # Try persisted managed-room identifiers so room key/alias permissions still
    # work when only room_id is available. This state is authored by MindRoom's
    # room-management flow, unlike arbitrary canonical aliases from Matrix room
    # events.
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
    if _is_sender_allowed_by_agent_reply_allowlist(sender_id, agent_name, config):
        return True

    # Internal MindRoom participants are not restricted by per-user reply lists.
    # Bridge bot accounts are intentionally not exempt.
    return sender_id in current_internal_sender_ids(config, runtime_paths)


def _is_sender_allowed_by_agent_reply_allowlist(sender_id: str, agent_name: str, config: Config) -> bool:
    """Check only the configured per-agent reply allowlist for one sender."""
    agent_reply_permissions = config.authorization.agent_reply_permissions
    allowed_users = agent_reply_permissions.get(agent_name)
    if allowed_users is None:
        allowed_users = agent_reply_permissions.get("*")
    if allowed_users is None:
        return True
    if "*" in allowed_users:
        return True

    resolved_sender = config.authorization.resolve_alias(sender_id)
    return any(fnmatchcase(resolved_sender, allowed_user) for allowed_user in allowed_users)


def is_sender_allowed_for_agent_credential_management(
    sender_id: str,
    agent_name: str,
    config: Config,
) -> bool:
    """Check whether a dashboard requester may manage credentials for one agent."""
    return _is_sender_allowed_by_agent_reply_allowlist(sender_id, agent_name, config)


def get_effective_sender_id_for_reply_permissions(
    sender_id: str,
    event_source: Mapping[str, Any] | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the sender ID used for per-agent reply permission checks.

    Internal MindRoom senders may relay user-originated messages (voice
    transcriptions, scheduled task fires, etc.) and include the original sender
    in event content. For trusted internal senders and trusted source kinds, use
    that embedded sender.
    """
    is_internal_mindroom_sender = sender_id in current_internal_sender_ids(config, runtime_paths)
    if not is_internal_mindroom_sender:
        return sender_id
    if not event_source:
        return sender_id

    content = event_source.get("content")
    if not isinstance(content, Mapping):
        return sender_id
    if not source_kind_allows_trusted_original_sender(source_kind_from_content(content)):
        return sender_id

    original_sender = content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        return original_sender
    return sender_id


def filter_responders_by_sender_permissions(
    responders: Sequence[MatrixID],
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return only responders that may reply to *sender_id* per config rules."""
    registry = entity_identity_registry(config, runtime_paths)
    result: list[MatrixID] = []
    for responder in responders:
        name = registry.current_entity_name_for_user_id(responder.full_id, include_router=False)
        if name is not None and is_sender_allowed_for_agent_reply(sender_id, name, config, runtime_paths):
            result.append(responder)
    return result


def _available_responders_from_member_ids(
    member_ids: Iterable[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return non-router responder IDs present in one membership snapshot."""
    registry = entity_identity_registry(config, runtime_paths)
    responders: list[MatrixID] = []
    for member_id in member_ids:
        entity_name = registry.current_entity_name_for_user_id(member_id, include_router=False)
        if entity_name is not None:
            responders.append(registry.current_id(entity_name))
    return sorted(responders, key=lambda x: x.full_id)


def get_available_responders_in_room(
    room: nio.MatrixRoom,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Get available responder Matrix IDs in a room.

    The router is excluded because it is not a regular conversation participant.
    """
    return _available_responders_from_member_ids(_joined_member_ids(room), config, runtime_paths)


def _joined_member_ids(room: nio.MatrixRoom) -> Iterable[str]:
    """Return cached room members that are joined rather than merely invited."""
    invited_user_ids = set(room.invited_users)
    return (user_id for user_id in room.users if user_id not in invited_user_ids)


def _get_available_responders_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return room responders that may reply to *sender_id*."""
    return filter_responders_by_sender_permissions(
        get_available_responders_in_room(room, config, runtime_paths),
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
            and not cached_user.invited
            and cached_user.display_name == member.display_name
            and cached_user.avatar_url == member.avatar_url
        ):
            continue
        if cached_user is not None:
            room.remove_member(member.user_id)
        room.add_member(member.user_id, member.display_name, member.avatar_url)

    room.members_synced = True


async def _get_available_responders_for_sender_authoritative(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return sender-visible room responders, refreshing membership while the cache is unsynced."""
    cached_room_responders = get_available_responders_in_room(room, config, runtime_paths)
    cached_visible_responders = filter_responders_by_sender_permissions(
        cached_room_responders,
        sender_id,
        config,
        runtime_paths,
    )
    if room.members_synced:
        return cached_visible_responders

    response = await client.joined_members(room.room_id)
    if not isinstance(response, nio.JoinedMembersResponse):
        logger.warning(
            "authoritative_room_membership_fetch_failed",
            room_id=room.room_id,
            sender_id=sender_id,
            error=str(response),
        )
        return cached_visible_responders

    _apply_authoritative_joined_members(room, response.members)
    refreshed_room_responders = _available_responders_from_member_ids(
        (member.user_id for member in response.members),
        config,
        runtime_paths,
    )
    refreshed_responders = filter_responders_by_sender_permissions(
        refreshed_room_responders,
        sender_id,
        config,
        runtime_paths,
    )
    logger.info(
        "authoritative_room_membership_refreshed",
        room_id=room.room_id,
        sender_id=sender_id,
        cached_responder_count=len(cached_room_responders),
        refreshed_responder_count=len(refreshed_responders),
    )
    return refreshed_responders


def responder_candidate_entities_from_cached_room(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return sender-visible responder candidates without refreshing Matrix membership."""
    configured_entities = _configured_responder_candidates_for_room(room, sender_id, config, runtime_paths)
    if configured_entities is not None:
        return configured_entities
    return _get_available_responders_for_sender(room, sender_id, config, runtime_paths)


def _configured_responder_candidates_for_room(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID] | None:
    """Return configured-room responder candidates, or None for ad-hoc rooms."""
    room_alias = room.canonical_alias
    room_aliases = (room_alias,) if isinstance(room_alias, str) and room_alias else ()
    configured_entities = configured_routable_entity_ids_for_room(
        config,
        room.room_id,
        runtime_paths,
        room_aliases=room_aliases,
    )
    if not configured_entities:
        return None
    return filter_responders_by_sender_permissions(configured_entities, sender_id, config, runtime_paths)


async def responder_candidate_entities_for_room(
    client: nio.AsyncClient | None,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return sender-visible responder candidates without widening configured rooms."""
    configured_entities = _configured_responder_candidates_for_room(room, sender_id, config, runtime_paths)
    if configured_entities is not None:
        return configured_entities
    if client is None:
        return _get_available_responders_for_sender(room, sender_id, config, runtime_paths)
    return await _get_available_responders_for_sender_authoritative(client, room, sender_id, config, runtime_paths)
