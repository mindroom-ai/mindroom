"""Authorization utilities for sender and per-agent access checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from mindroom.access_policy import resolve_responder_access
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.dispatch_source import source_kind_allows_trusted_original_sender, source_kind_from_content
from mindroom.entity_resolution import (
    MissingManagedEntityAccountError,
    configured_routable_entity_ids_for_room,
    current_internal_sender_ids,
    entity_identity_registry,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.room_membership import cached_joined_member_ids, ensure_room_membership_synced
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID


logger = get_logger(__name__)


class _ReplyAuthorizationDecision(Enum):
    """Separate a proven denial from temporarily unresolved membership."""

    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"


class ReplyMembershipPendingError(RuntimeError):
    """The exact journal source must retry after authoritative membership refresh."""

    def __init__(self) -> None:
        super().__init__("Reply authorization awaits authoritative room membership")


def is_sender_allowed_for_responder(
    sender_id: str,
    entity_name: str,
    room_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    *,
    require_resolved_membership: bool = False,
) -> bool:
    """Apply the complete membership policy, failing closed on uncertainty."""
    decision = _responder_reply_authorization(
        sender_id,
        entity_name,
        room_id,
        config,
        runtime_paths,
        membership_index,
    )
    if require_resolved_membership and decision is _ReplyAuthorizationDecision.PENDING:
        raise ReplyMembershipPendingError
    return decision is _ReplyAuthorizationDecision.ALLOWED


def _responder_reply_authorization(
    sender_id: str,
    entity_name: str,
    room_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> _ReplyAuthorizationDecision:
    """Allow any proven grant before considering unresolved relevant membership."""
    allowed = sender_id in _current_internal_sender_ids_for_auth(config, runtime_paths)
    resolved_sender = resolve_human_requester_alias(sender_id, config, runtime_paths)
    access = resolve_responder_access(config, entity_name)
    allowed = allowed or resolved_sender in config.administrators
    allowed = allowed or any(fnmatchcase(resolved_sender, allowed_user) for allowed_user in access.users)
    allowed = allowed or (
        room_id is not None
        and access.current_room_members
        and membership_index.is_current_room_member(resolved_sender, room_id, config, runtime_paths)
    )
    allowed = allowed or (
        bool(access.members_of_rooms)
        and membership_index.is_allowed(
            resolved_sender,
            access.members_of_rooms,
            config,
            runtime_paths,
        )
    )
    if allowed:
        return _ReplyAuthorizationDecision.ALLOWED
    if membership_index.grants_pending(
        config,
        joined_rooms=access.members_of_rooms,
        current_room_id=room_id if access.current_room_members else None,
    ):
        return _ReplyAuthorizationDecision.PENDING
    return _ReplyAuthorizationDecision.DENIED


def is_sender_allowed_for_agent_reply_in_room(
    sender_id: str,
    agent_name: str,
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    *,
    require_resolved_membership: bool = False,
) -> bool:
    """Require both current-room access and entity reply access."""
    return is_sender_allowed_for_entity_replies_in_room(
        sender_id,
        (agent_name,),
        config,
        room_id,
        runtime_paths,
        membership_index,
        require_resolved_membership=require_resolved_membership,
    )


def is_sender_allowed_for_entity_replies_in_room(
    sender_id: str,
    entity_names: Iterable[str],
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    *,
    require_resolved_membership: bool = False,
) -> bool:
    """Require membership access for every execution entity."""
    decisions = {
        _responder_reply_authorization(
            sender_id,
            entity_name,
            room_id,
            config,
            runtime_paths,
            membership_index,
        )
        for entity_name in entity_names
    }
    if _ReplyAuthorizationDecision.DENIED in decisions:
        return False
    if _ReplyAuthorizationDecision.PENDING in decisions:
        if require_resolved_membership:
            raise ReplyMembershipPendingError
        return False
    return True


def _current_internal_sender_ids_for_auth(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return internal sender IDs when prepared, or an empty set before provisioning."""
    try:
        return current_internal_sender_ids(config, runtime_paths)
    except MissingManagedEntityAccountError:
        logger.debug("managed_entity_accounts_unavailable_for_auth_check")
        return frozenset()


def is_sender_allowed_for_agent_credential_management(
    sender_id: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Check whether a dashboard requester may manage credentials for one agent."""
    agent = config.agents.get(agent_name)
    if agent is None:
        return False
    resolved_sender = resolve_human_requester_alias(sender_id, config, runtime_paths)
    return resolved_sender in config.administrators or resolved_sender in agent.credential_managers


def is_sender_allowed_for_agent_oauth_connection_management(
    sender_id: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Check whether a requester may manage their OAuth connection for one agent.

    Callers granting the private-agent exception must resolve credentials to the
    authenticated requester's isolated user or user-agent target.
    """
    if not sender_id:
        return False
    agent = config.agents.get(agent_name)
    if agent is None:
        return False
    if agent.private is not None:
        return True
    return is_sender_allowed_for_agent_credential_management(sender_id, agent_name, config, runtime_paths)


def is_platform_administrator(sender_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether a requester has platform-wide administrative authority."""
    resolved_sender = resolve_human_requester_alias(sender_id, config, runtime_paths)
    return resolved_sender in config.administrators


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
    membership_index: AgentReplyMembershipIndex,
    room_id: str | None = None,
) -> list[MatrixID]:
    """Return only responders that may reply to *sender_id* per config rules."""
    registry = entity_identity_registry(config, runtime_paths)
    result: list[MatrixID] = []
    for responder in responders:
        name = registry.current_entity_name_for_user_id(responder.full_id, include_router=False)
        if name is not None and is_sender_allowed_for_responder(
            sender_id,
            name,
            room_id,
            config,
            runtime_paths,
            membership_index,
        ):
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
    return _available_responders_from_member_ids(cached_joined_member_ids(room), config, runtime_paths)


def _get_available_responders_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> list[MatrixID]:
    """Return room responders that may reply to *sender_id*."""
    return filter_responders_by_sender_permissions(
        get_available_responders_in_room(room, config, runtime_paths),
        sender_id,
        config,
        runtime_paths,
        membership_index,
        room.room_id,
    )


async def _get_available_responders_for_sender_authoritative(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> list[MatrixID]:
    """Return sender-visible room responders, refreshing membership while the cache is unsynced.

    A failed refresh leaves the cached members untouched, so the same
    cache-backed resolution serves both outcomes.
    """
    await ensure_room_membership_synced(client, room, sender_id=sender_id)
    return _get_available_responders_for_sender(room, sender_id, config, runtime_paths, membership_index)


@dataclass(frozen=True)
class ResponderCandidatePermissions:
    """Separate proven candidates from unresolved grants at durable planning."""

    allowed: list[MatrixID]
    pending: list[MatrixID]


def classify_responder_candidates_from_cached_room(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> ResponderCandidatePermissions:
    """Preserve membership uncertainty without deciding which candidates matter."""
    responders = _configured_responder_entities_for_room(room, config, runtime_paths)
    if responders is None:
        responders = get_available_responders_in_room(room, config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    allowed: list[MatrixID] = []
    pending: list[MatrixID] = []
    for responder in responders:
        name = registry.current_entity_name_for_user_id(responder.full_id, include_router=False)
        if name is None:
            continue
        decision = _responder_reply_authorization(
            sender_id,
            name,
            room.room_id,
            config,
            runtime_paths,
            membership_index,
        )
        if decision is _ReplyAuthorizationDecision.ALLOWED:
            allowed.append(responder)
        elif decision is _ReplyAuthorizationDecision.PENDING:
            pending.append(responder)
    return ResponderCandidatePermissions(allowed, pending)


def responder_candidate_entities_from_cached_room(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> list[MatrixID]:
    """Return sender-visible responder candidates without refreshing Matrix membership."""
    return classify_responder_candidates_from_cached_room(
        room,
        sender_id,
        config,
        runtime_paths,
        membership_index,
    ).allowed


def _configured_responder_entities_for_room(
    room: nio.MatrixRoom,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID] | None:
    """Return configured-room responders before permissions, or None for ad-hoc rooms."""
    room_alias = room.canonical_alias
    room_aliases = (room_alias,) if isinstance(room_alias, str) and room_alias else ()
    return (
        configured_routable_entity_ids_for_room(
            config,
            room.room_id,
            runtime_paths,
            room_aliases=room_aliases,
        )
        or None
    )


async def responder_candidate_entities_with_membership_refresh(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> list[MatrixID]:
    """Return candidates, refreshing unsynced ad-hoc room membership when possible."""
    configured_entities = _configured_responder_entities_for_room(room, config, runtime_paths)
    if configured_entities is not None:
        return filter_responders_by_sender_permissions(
            configured_entities,
            sender_id,
            config,
            runtime_paths,
            membership_index,
            room.room_id,
        )
    return await _get_available_responders_for_sender_authoritative(
        client,
        room,
        sender_id,
        config,
        runtime_paths,
        membership_index,
    )
