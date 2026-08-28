"""Authoritative in-memory room-membership grants for entity replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import nio

from mindroom.access_policy import resolve_responder_access
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.state import matrix_state_for_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

type _ResponderMembershipPolicySignature = tuple[str, bool, tuple[str, ...]]
type _AgentReplyMembershipPolicySignature = tuple[_ResponderMembershipPolicySignature, ...]


def _agent_reply_membership_policy_signature(
    config: Config,
) -> _AgentReplyMembershipPolicySignature:
    """Return the configured room grants that determine membership snapshots."""
    entity_names = (*config.agents, *config.teams, ROUTER_AGENT_NAME)
    return tuple(
        sorted(
            (
                entity_name,
                access.current_room_members,
                tuple(sorted(access.members_of_rooms)),
            )
            for entity_name in entity_names
            for access in (resolve_responder_access(config, entity_name),)
        ),
    )


def agent_reply_membership_policy_changed(
    current: Config,
    replacement: Config,
) -> bool:
    """Return whether a config replacement changes membership snapshot inputs."""
    return _agent_reply_membership_policy_signature(current) != _agent_reply_membership_policy_signature(replacement)


def _referenced_room_keys(config: Config) -> tuple[str, ...]:
    """Return distinct managed grant-room keys in deterministic order."""
    signature = _agent_reply_membership_policy_signature(config)
    return tuple(sorted({room_key for _, _, room_keys in signature for room_key in room_keys}))


def _requires_current_room_memberships(config: Config) -> bool:
    """Return whether any active responder policy reads current-room membership."""
    return any(current_room_members for _, current_room_members, _ in _agent_reply_membership_policy_signature(config))


@dataclass(frozen=True, slots=True)
class _GrantRoomMembership:
    """One managed grant room's stable identity and joined-user snapshot."""

    room_key: str | None
    room_id: str | None
    ready: bool
    raw_joined_user_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _AgentReplyMembershipSnapshot:
    """One atomically published view of every configured grant room."""

    policy_signature: _AgentReplyMembershipPolicySignature | None = None
    rooms: tuple[_GrantRoomMembership, ...] = ()
    refresh_required: bool = True


class AgentReplyMembershipIndex:
    """Own the process-local membership state used by reply authorization."""

    def __init__(self) -> None:
        self._snapshot = _AgentReplyMembershipSnapshot()
        self._desired_signature: _AgentReplyMembershipPolicySignature | None = None
        self._epoch = 0
        self._refresh_lock = asyncio.Lock()

    @property
    def snapshot(self) -> _AgentReplyMembershipSnapshot:
        """Return the current immutable snapshot."""
        return self._snapshot

    def needs_refresh(self, config: Config) -> bool:
        """Return whether room-backed grants need an authoritative rebuild."""
        if not _referenced_room_keys(config) and not _requires_current_room_memberships(config):
            return False
        return (
            self._snapshot.policy_signature != _agent_reply_membership_policy_signature(config)
            or self._snapshot.refresh_required
        )

    def is_allowed(
        self,
        sender_id: str,
        joined_rooms: Sequence[str],
        config: Config,
    ) -> bool:
        """Return whether a sender is joined to any ready configured grant room."""
        snapshot = self._snapshot
        if snapshot.policy_signature != _agent_reply_membership_policy_signature(config):
            return False
        allowed_room_keys = frozenset(joined_rooms)
        return any(
            room.ready
            and room.room_key in allowed_room_keys
            and _raw_membership_matches_sender(room.raw_joined_user_ids, sender_id, config)
            for room in snapshot.rooms
        )

    def is_current_room_member(self, sender_id: str, room_id: str, config: Config) -> bool:
        """Return whether the authoritative snapshot joins a sender to one current room."""
        snapshot = self._snapshot
        if snapshot.policy_signature != _agent_reply_membership_policy_signature(config):
            return False
        return any(
            room.ready
            and room.room_id == room_id
            and _raw_membership_matches_sender(room.raw_joined_user_ids, sender_id, config)
            for room in snapshot.rooms
        )

    def invalidate(self, config: Config, *, reason: str) -> None:
        """Revoke every room-backed grant until an authoritative refresh succeeds."""
        previous_rooms = {room.room_key: room for room in self._snapshot.rooms}
        room_keys = _referenced_room_keys(config)
        requires_current_rooms = _requires_current_room_memberships(config)
        signature = _agent_reply_membership_policy_signature(config)
        self._desired_signature = signature
        self._epoch += 1
        grant_rooms = tuple(
            _GrantRoomMembership(
                room_key=room_key,
                room_id=previous_rooms[room_key].room_id if room_key in previous_rooms else None,
                ready=False,
            )
            for room_key in room_keys
        )
        grant_room_ids = {room.room_id for room in grant_rooms}
        current_rooms = (
            tuple(
                replace(room, ready=False, raw_joined_user_ids=frozenset())
                for room in self._snapshot.rooms
                if room.room_key is None and room.room_id not in grant_room_ids
            )
            if requires_current_rooms
            else ()
        )
        self._snapshot = _AgentReplyMembershipSnapshot(
            policy_signature=signature,
            rooms=(*grant_rooms, *current_rooms),
            refresh_required=bool(room_keys) or requires_current_rooms,
        )
        logger.info(
            "agent_reply_memberships_invalidated",
            reason=reason,
            grant_room_count=len(room_keys),
        )

    def mark_control_room_unready(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        room_id: str,
        *,
        reason: str,
    ) -> bool:
        """Fail one grant room closed after the control client departs it."""
        signature = _agent_reply_membership_policy_signature(config)
        if self._desired_signature != signature or self._snapshot.policy_signature != signature:
            return False
        state = matrix_state_for_runtime(runtime_paths)
        matching_room_keys = {
            room_key
            for room_key in _referenced_room_keys(config)
            if (managed_room := state.rooms.get(room_key)) is not None and managed_room.room_id == room_id
        }
        matching_rooms = tuple(
            room for room in self._snapshot.rooms if room.room_id == room_id or room.room_key in matching_room_keys
        )
        if not matching_rooms and not _requires_current_room_memberships(config):
            return False

        # A departure observed while an authoritative query is in flight must
        # fence that query even when the room ID was not yet published.
        self._epoch += 1
        updated_rooms = [
            replace(
                room,
                room_id=room_id,
                ready=False,
                raw_joined_user_ids=frozenset(),
            )
            if room in matching_rooms
            else room
            for room in self._snapshot.rooms
        ]
        if not matching_rooms:
            updated_rooms.append(_GrantRoomMembership(room_key=None, room_id=room_id, ready=False))
        self._snapshot = replace(
            self._snapshot,
            rooms=tuple(updated_rooms),
            refresh_required=True,
        )
        logged_rooms = matching_rooms or (updated_rooms[-1],)
        for matched_room in logged_rooms:
            logger.warning(
                "agent_reply_grant_room_unready",
                room_key=matched_room.room_key,
                room_id=room_id,
                readiness="unready",
                reason=reason,
            )
        return True

    async def refresh(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        client: nio.AsyncClient,
    ) -> None:
        """Atomically replace membership state from authoritative Matrix queries."""
        signature = _agent_reply_membership_policy_signature(config)
        if self._desired_signature is None:
            self._desired_signature = signature
        elif self._desired_signature != signature:
            self.invalidate(config, reason="policy_changed_before_refresh")
        async with self._refresh_lock:
            expected_epoch = self._epoch
            previous_rooms = (
                self._snapshot.rooms
                if self._snapshot.policy_signature == signature
                and self._snapshot.refresh_required
                and any(not room.ready for room in self._snapshot.rooms)
                else ()
            )
            candidate = await _build_authoritative_snapshot(
                config,
                runtime_paths,
                client,
                signature=signature,
                previous_rooms=previous_rooms,
            )
            if self._desired_signature != signature or self._epoch != expected_epoch:
                return
            self._snapshot = candidate

    def apply_member_event(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
    ) -> bool:
        """Apply one live membership transition to every matching ready grant room."""
        signature = _agent_reply_membership_policy_signature(config)
        if self._desired_signature != signature:
            return False

        snapshot = self._snapshot
        if snapshot.policy_signature != signature:
            return False

        matching_rooms = tuple(room for room in snapshot.rooms if room.room_id == room_id)
        if not matching_rooms:
            state = matrix_state_for_runtime(runtime_paths)
            matching_rooms = tuple(
                room
                for room in snapshot.rooms
                if room.room_key is not None
                and (managed_room := state.rooms.get(room.room_key)) is not None
                and managed_room.room_id == room_id
            )
        if not matching_rooms and not _requires_current_room_memberships(config):
            return False

        # A newly configured room can still lack an ID in the fail-closed
        # snapshot. Its persisted managed-room identity nevertheless proves
        # that a live transition must fence any older query in flight.
        self._epoch += 1

        updated_rooms = [
            _apply_transition_to_room(
                replace(room, room_id=room_id) if room in matching_rooms else room,
                room_id=room_id,
                event=event,
                control_user_id=control_user_id,
            )
            for room in snapshot.rooms
        ]
        if not matching_rooms:
            updated_rooms.append(_GrantRoomMembership(room_key=None, room_id=room_id, ready=False))
        updated_rooms_tuple = tuple(updated_rooms)
        changed_room_keys = [
            previous.room_key
            for previous, updated in zip(snapshot.rooms, updated_rooms_tuple, strict=False)
            if previous != updated
        ]
        if len(updated_rooms_tuple) > len(snapshot.rooms):
            changed_room_keys.append(None)

        if not changed_room_keys:
            return False
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
        return True


def _apply_transition_to_room(
    room: _GrantRoomMembership,
    *,
    room_id: str,
    event: nio.RoomMemberEvent,
    control_user_id: str,
) -> _GrantRoomMembership:
    """Return one grant-room value after applying a matching live transition."""
    if room.room_id != room_id:
        return room
    if event.state_key == control_user_id and event.membership != "join":
        return replace(
            room,
            ready=False,
            raw_joined_user_ids=frozenset(),
        )
    if not room.ready:
        return room
    raw_joined_user_ids = set(room.raw_joined_user_ids)
    if event.membership == "join":
        raw_joined_user_ids.add(event.state_key)
    else:
        raw_joined_user_ids.discard(event.state_key)
    return replace(room, raw_joined_user_ids=frozenset(raw_joined_user_ids))


async def _build_authoritative_snapshot(
    config: Config,
    runtime_paths: RuntimePaths,
    client: nio.AsyncClient,
    *,
    signature: _AgentReplyMembershipPolicySignature,
    previous_rooms: tuple[_GrantRoomMembership, ...],
) -> _AgentReplyMembershipSnapshot:
    """Build one complete candidate without exposing partially refreshed rooms."""
    room_keys = _referenced_room_keys(config)
    requires_current_rooms = _requires_current_room_memberships(config)
    if not room_keys and not requires_current_rooms:
        return _AgentReplyMembershipSnapshot(policy_signature=signature, refresh_required=False)

    state = matrix_state_for_runtime(runtime_paths)
    joined_room_ids = await _authoritative_joined_room_ids(client)
    memberships_by_room_id: dict[str, frozenset[str] | None] = {}
    rooms: list[_GrantRoomMembership] = []
    previous_grant_rooms = {room.room_key: room for room in previous_rooms if room.room_key is not None}
    for room_key in room_keys:
        managed_room = state.rooms.get(room_key)
        room_id = managed_room.room_id if managed_room is not None else None
        previous_room = previous_grant_rooms.get(room_key)
        if (
            previous_room is not None
            and previous_room.ready
            and previous_room.room_id == room_id
            and joined_room_ids is not None
            and room_id in joined_room_ids
        ):
            rooms.append(previous_room)
            continue
        rooms.append(
            await _build_grant_room_membership(
                client,
                room_key=room_key,
                room_id=room_id,
                joined_room_ids=joined_room_ids,
                memberships_by_room_id=memberships_by_room_id,
            ),
        )
    grant_room_ids = {room.room_id for room in rooms if room.room_id is not None}
    if requires_current_rooms and joined_room_ids is not None:
        rooms.extend(
            await _build_current_room_memberships(
                client,
                joined_room_ids - grant_room_ids,
                previous_rooms=previous_rooms,
            ),
        )
    frozen_rooms = tuple(rooms)
    return _AgentReplyMembershipSnapshot(
        policy_signature=signature,
        rooms=frozen_rooms,
        refresh_required=(joined_room_ids is None or any(not room.ready for room in frozen_rooms)),
    )


async def _build_grant_room_membership(
    client: nio.AsyncClient,
    *,
    room_key: str,
    room_id: str | None,
    joined_room_ids: frozenset[str] | None,
    memberships_by_room_id: dict[str, frozenset[str] | None],
) -> _GrantRoomMembership:
    """Build one managed grant-room snapshot without publishing partial state."""
    if room_id is None:
        return _unready_room(room_key, None, reason="managed_room_unresolved")
    if joined_room_ids is None:
        return _unready_room(room_key, room_id, reason="joined_rooms_unavailable")
    if room_id not in joined_room_ids:
        return _unready_room(room_key, room_id, reason="control_client_not_joined")
    if room_id not in memberships_by_room_id:
        memberships_by_room_id[room_id] = await _authoritative_room_members(
            client,
            room_key=room_key,
            room_id=room_id,
        )
    raw_joined_user_ids = memberships_by_room_id[room_id]
    if raw_joined_user_ids is None:
        return _unready_room(room_key, room_id, reason="joined_members_unavailable")
    logger.info(
        "agent_reply_grant_room_ready",
        room_key=room_key,
        room_id=room_id,
        readiness="ready",
        member_count=len(raw_joined_user_ids),
    )
    return _GrantRoomMembership(
        room_key=room_key,
        room_id=room_id,
        ready=True,
        raw_joined_user_ids=raw_joined_user_ids,
    )


async def _build_current_room_memberships(
    client: nio.AsyncClient,
    room_ids: frozenset[str],
    *,
    previous_rooms: tuple[_GrantRoomMembership, ...],
) -> list[_GrantRoomMembership]:
    """Build authoritative snapshots for joined rooms used by current-room grants."""
    previous_by_room_id = {
        room.room_id: room
        for room in previous_rooms
        if room.room_key is None and room.room_id is not None and room.ready
    }
    rooms: list[_GrantRoomMembership] = []
    for room_id in sorted(room_ids):
        if previous_room := previous_by_room_id.get(room_id):
            rooms.append(previous_room)
            continue
        raw_joined_user_ids = await _authoritative_room_members(
            client,
            room_key=room_id,
            room_id=room_id,
        )
        if raw_joined_user_ids is None:
            rooms.append(_unready_room(None, room_id, reason="joined_members_unavailable"))
            continue
        rooms.append(
            _GrantRoomMembership(
                room_key=None,
                room_id=room_id,
                ready=True,
                raw_joined_user_ids=raw_joined_user_ids,
            ),
        )
    return rooms


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


def _raw_membership_matches_sender(
    raw_user_ids: frozenset[str],
    sender_id: str,
    config: Config,
) -> bool:
    """Match current alias equivalence against the raw Matrix membership roster."""
    authorization = config.authorization
    canonical_sender = authorization.resolve_alias(sender_id)
    equivalent_user_ids = {canonical_sender, *authorization.aliases.get(canonical_sender, ())}
    return not raw_user_ids.isdisjoint(equivalent_user_ids)


def _unready_room(room_key: str | None, room_id: str | None, *, reason: str) -> _GrantRoomMembership:
    """Build and log one fail-closed grant-room snapshot."""
    logger.warning(
        "agent_reply_grant_room_unready",
        room_key=room_key,
        room_id=room_id,
        readiness="unready",
        reason=reason,
    )
    return _GrantRoomMembership(room_key=room_key, room_id=room_id, ready=False)
