"""Authoritative in-memory room-membership grants for entity replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.state import matrix_state_for_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

type AgentReplyMembershipPolicySignature = tuple[
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
]


def agent_reply_membership_policy_signature(
    authorization: AuthorizationConfig,
) -> AgentReplyMembershipPolicySignature:
    """Return the policy inputs that determine canonical membership grants."""
    room_grants = tuple(
        sorted(
            (entity_name, tuple(sorted(policy.joined_rooms)))
            for entity_name, policy in authorization.agent_reply_permissions.items()
        ),
    )
    aliases = tuple(
        sorted(
            (canonical_user_id, tuple(sorted(alias_user_ids)))
            for canonical_user_id, alias_user_ids in authorization.aliases.items()
        ),
    )
    return room_grants, aliases


def _referenced_room_keys(authorization: AuthorizationConfig) -> tuple[str, ...]:
    """Return distinct managed grant-room keys in deterministic order."""
    return tuple(
        sorted(
            {
                room_key
                for policy in authorization.agent_reply_permissions.values()
                for room_key in policy.joined_rooms
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class GrantRoomMembership:
    """One managed grant room's stable identity and joined-user snapshot."""

    room_key: str
    room_id: str | None
    ready: bool
    raw_joined_user_ids: frozenset[str] = frozenset()
    joined_user_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AgentReplyMembershipSnapshot:
    """One atomically published view of every configured grant room."""

    policy_signature: AgentReplyMembershipPolicySignature | None = None
    rooms: tuple[GrantRoomMembership, ...] = ()
    refresh_required: bool = True


class AgentReplyMembershipIndex:
    """Own the process-local membership state used by reply authorization."""

    def __init__(self) -> None:
        self._snapshot = AgentReplyMembershipSnapshot()
        self._desired_signature: AgentReplyMembershipPolicySignature | None = None
        self._epoch = 0
        self._refresh_lock = asyncio.Lock()

    @property
    def snapshot(self) -> AgentReplyMembershipSnapshot:
        """Return the current immutable snapshot."""
        return self._snapshot

    def needs_refresh(self, authorization: AuthorizationConfig) -> bool:
        """Return whether room-backed grants need an authoritative rebuild."""
        if not _referenced_room_keys(authorization):
            return False
        return (
            self._snapshot.policy_signature != agent_reply_membership_policy_signature(authorization)
            or self._snapshot.refresh_required
        )

    def is_allowed(
        self,
        sender_id: str,
        joined_rooms: Sequence[str],
        authorization: AuthorizationConfig,
    ) -> bool:
        """Return whether a sender is joined to any ready configured grant room."""
        snapshot = self._snapshot
        if snapshot.policy_signature != agent_reply_membership_policy_signature(authorization):
            return False
        resolved_sender = authorization.resolve_alias(sender_id)
        allowed_room_keys = frozenset(joined_rooms)
        return any(
            room.ready
            and room.room_key in allowed_room_keys
            and resolved_sender in room.joined_user_ids
            for room in snapshot.rooms
        )

    def invalidate(self, config: Config, *, reason: str) -> None:
        """Revoke every room-backed grant until an authoritative refresh succeeds."""
        previous_rooms = {room.room_key: room for room in self._snapshot.rooms}
        room_keys = _referenced_room_keys(config.authorization)
        signature = agent_reply_membership_policy_signature(config.authorization)
        self._desired_signature = signature
        self._epoch += 1
        self._snapshot = AgentReplyMembershipSnapshot(
            policy_signature=signature,
            rooms=tuple(
                GrantRoomMembership(
                    room_key=room_key,
                    room_id=(previous_rooms.get(room_key).room_id if room_key in previous_rooms else None),
                    ready=False,
                )
                for room_key in room_keys
            ),
            refresh_required=bool(room_keys),
        )
        logger.info(
            "agent_reply_memberships_invalidated",
            reason=reason,
            grant_room_count=len(room_keys),
        )

    async def refresh(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        client: nio.AsyncClient,
    ) -> None:
        """Atomically replace membership state from authoritative Matrix queries."""
        authorization = config.authorization
        signature = agent_reply_membership_policy_signature(authorization)
        if self._desired_signature is None:
            self._desired_signature = signature
        if self._desired_signature != signature:
            return
        async with self._refresh_lock:
            while self._desired_signature == signature:
                expected_epoch = self._epoch
                candidate = await _build_authoritative_snapshot(
                    config,
                    runtime_paths,
                    client,
                    signature=signature,
                )
                if self._desired_signature != signature:
                    return
                if self._epoch != expected_epoch:
                    continue
                self._snapshot = candidate
                return

    def apply_member_event(
        self,
        config: Config,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
    ) -> None:
        """Apply one live membership transition to every matching ready grant room."""
        snapshot = self._snapshot
        if snapshot.policy_signature != agent_reply_membership_policy_signature(config.authorization):
            return

        matching_room_keys = tuple(room.room_key for room in snapshot.rooms if room.room_id == room_id)
        if not matching_room_keys:
            return
        self._epoch += 1

        changed_room_keys: list[str] = []
        updated_rooms: list[GrantRoomMembership] = []
        for room in snapshot.rooms:
            if room.room_id != room_id:
                updated_rooms.append(room)
                continue
            if event.state_key == control_user_id and event.membership != "join":
                updated_room = replace(
                    room,
                    ready=False,
                    raw_joined_user_ids=frozenset(),
                    joined_user_ids=frozenset(),
                )
                updated_rooms.append(updated_room)
                if updated_room != room:
                    changed_room_keys.append(room.room_key)
                continue
            if not room.ready:
                updated_rooms.append(room)
                continue
            raw_joined_user_ids = set(room.raw_joined_user_ids)
            if event.membership == "join":
                raw_joined_user_ids.add(event.state_key)
            else:
                raw_joined_user_ids.discard(event.state_key)
            frozen_raw_user_ids = frozenset(raw_joined_user_ids)
            updated_room = replace(
                room,
                raw_joined_user_ids=frozen_raw_user_ids,
                joined_user_ids=_canonical_user_ids(frozen_raw_user_ids, config.authorization),
            )
            updated_rooms.append(updated_room)
            if updated_room != room:
                changed_room_keys.append(room.room_key)

        if not changed_room_keys:
            return
        updated_rooms_tuple = tuple(updated_rooms)
        self._snapshot = replace(
            snapshot,
            rooms=updated_rooms_tuple,
            refresh_required=any(not room.ready for room in updated_rooms_tuple),
        )
        for room_key in changed_room_keys:
            logger.info(
                "agent_reply_grant_room_membership_transition",
                room_key=room_key,
                room_id=room_id,
                membership=event.membership,
                transition=("grant" if event.membership == "join" else "revoke"),
                authorization_source="joined_room",
            )


async def _build_authoritative_snapshot(
    config: Config,
    runtime_paths: RuntimePaths,
    client: nio.AsyncClient,
    *,
    signature: AgentReplyMembershipPolicySignature,
) -> AgentReplyMembershipSnapshot:
    """Build one complete candidate without exposing partially refreshed rooms."""
    authorization = config.authorization
    room_keys = _referenced_room_keys(authorization)
    if not room_keys:
        return AgentReplyMembershipSnapshot(policy_signature=signature, refresh_required=False)

    state = matrix_state_for_runtime(runtime_paths)
    joined_room_ids = await _authoritative_joined_room_ids(client)
    memberships_by_room_id: dict[str, frozenset[str] | None] = {}
    rooms: list[GrantRoomMembership] = []
    for room_key in room_keys:
        managed_room = state.rooms.get(room_key)
        if managed_room is None:
            rooms.append(_unready_room(room_key, None, reason="managed_room_unresolved"))
            continue
        room_id = managed_room.room_id
        if joined_room_ids is None:
            rooms.append(_unready_room(room_key, room_id, reason="joined_rooms_unavailable"))
            continue
        if room_id not in joined_room_ids:
            rooms.append(_unready_room(room_key, room_id, reason="control_client_not_joined"))
            continue
        if room_id not in memberships_by_room_id:
            memberships_by_room_id[room_id] = await _authoritative_room_members(
                client,
                room_key=room_key,
                room_id=room_id,
            )
        raw_joined_user_ids = memberships_by_room_id[room_id]
        if raw_joined_user_ids is None:
            rooms.append(_unready_room(room_key, room_id, reason="joined_members_unavailable"))
            continue
        joined_user_ids = _canonical_user_ids(raw_joined_user_ids, authorization)
        rooms.append(
            GrantRoomMembership(
                room_key=room_key,
                room_id=room_id,
                ready=True,
                raw_joined_user_ids=raw_joined_user_ids,
                joined_user_ids=joined_user_ids,
            ),
        )
        logger.info(
            "agent_reply_grant_room_ready",
            room_key=room_key,
            room_id=room_id,
            readiness="ready",
            member_count=len(joined_user_ids),
        )
    frozen_rooms = tuple(rooms)
    return AgentReplyMembershipSnapshot(
        policy_signature=signature,
        rooms=frozen_rooms,
        refresh_required=any(not room.ready for room in frozen_rooms),
    )


async def _authoritative_joined_room_ids(client: nio.AsyncClient) -> frozenset[str] | None:
    """Return the control client's joined rooms or fail closed."""
    try:
        response = await client.joined_rooms()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "agent_reply_control_client_joined_rooms_failed",
            readiness="unready",
            error=str(exc),
        )
        return None
    if isinstance(response, nio.JoinedRoomsResponse):
        return frozenset(response.rooms)
    logger.warning(
        "agent_reply_control_client_joined_rooms_failed",
        readiness="unready",
        error=str(response),
    )
    return None


async def _authoritative_room_members(
    client: nio.AsyncClient,
    *,
    room_key: str,
    room_id: str,
) -> frozenset[str] | None:
    """Return raw joined Matrix user IDs for one stable room ID or fail closed."""
    try:
        response = await client.joined_members(room_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "agent_reply_grant_room_snapshot_failed",
            room_key=room_key,
            room_id=room_id,
            readiness="unready",
            error=str(exc),
        )
        return None
    if not isinstance(response, nio.JoinedMembersResponse):
        logger.warning(
            "agent_reply_grant_room_snapshot_failed",
            room_key=room_key,
            room_id=room_id,
            readiness="unready",
            error=str(response),
        )
        return None
    return frozenset(member.user_id for member in response.members)


def _canonical_user_ids(
    raw_user_ids: frozenset[str],
    authorization: AuthorizationConfig,
) -> frozenset[str]:
    """Resolve raw room members without losing alias multiplicity for transitions."""
    return frozenset(authorization.resolve_alias(user_id) for user_id in raw_user_ids)


def _unready_room(room_key: str, room_id: str | None, *, reason: str) -> GrantRoomMembership:
    """Build and log one fail-closed grant-room snapshot."""
    logger.warning(
        "agent_reply_grant_room_unready",
        room_key=room_key,
        room_id=room_id,
        readiness="unready",
        reason=reason,
    )
    return GrantRoomMembership(room_key=room_key, room_id=room_id, ready=False)
