"""Leaf helpers for nio's encrypted room recipient roster."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import nio

from mindroom.matrix.response_status import (
    matrix_response_is_not_found,
    matrix_response_transport_succeeded,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass(slots=True)
class _ClientEncryptionState:
    """Mutable encryption generations owned by one nio client."""

    deferred_session_retirements: set[str] = field(default_factory=set)
    recipient_epochs: dict[str, int] = field(default_factory=dict)
    membership_epochs: dict[str, int] = field(default_factory=dict)
    joined_members_query_sequences: dict[str, int] = field(default_factory=dict)
    device_key_epoch: int = 0
    key_query_sequence: int = 0
    applied_key_query_sequence: int = 0


_CLIENT_ENCRYPTION_STATES: WeakKeyDictionary[nio.AsyncClient, _ClientEncryptionState] = WeakKeyDictionary()
_CLIENT_ENCRYPTION_STATES_GUARD = Lock()


@dataclass(frozen=True, slots=True)
class _RoomDeliveryGuard:
    client: nio.AsyncClient
    room_id: str
    room: nio.MatrixRoom | None
    encrypted: bool
    recipient_user_ids: frozenset[str] | None
    recipient_epoch: int
    membership_epoch: int


@dataclass(slots=True)
class _JoinedMembersQuery:
    """One request-local joined-members generation checkpoint."""

    client: nio.AsyncClient
    room_id: str
    sequence: int
    start_epoch: int
    response: nio.JoinedMembersResponse | None = None
    response_accepted: bool = False


@dataclass(frozen=True, slots=True)
class _KeyQueryRequest:
    """One request-local device-key generation checkpoint."""

    client: nio.AsyncClient
    sequence: int
    device_key_epoch: int
    user_ids: frozenset[str]


_ACTIVE_ROOM_DELIVERY_GUARD: ContextVar[_RoomDeliveryGuard | None] = ContextVar(
    "mindroom_active_room_delivery_guard",
    default=None,
)
_ACTIVE_JOINED_MEMBERS_QUERY: ContextVar[_JoinedMembersQuery | None] = ContextVar(
    "mindroom_active_joined_members_query",
    default=None,
)
_ACTIVE_KEY_QUERY_REQUEST: ContextVar[_KeyQueryRequest | None] = ContextVar(
    "mindroom_active_key_query_request",
    default=None,
)


def apply_authoritative_joined_roster(
    room: nio.MatrixRoom,
    members: nio.JoinedMembersResponse,
) -> frozenset[str]:
    """Replace nio's encryption recipients with authoritative joined members."""
    joined_user_ids = frozenset(member.user_id for member in members.members)
    for user_id in tuple(room.users):
        if user_id not in joined_user_ids or room.users[user_id].invited:
            room.remove_member(user_id)
    for member in members.members:
        room.add_member(member.user_id, member.display_name, member.avatar_url)
    room.members_synced = True
    return joined_user_ids


def joined_only_recipient_user_ids(room: nio.MatrixRoom) -> frozenset[str] | None:
    """Return nio's recipient IDs only when every entry is joined."""
    if room.invited_users or any(user.invited for user in room.users.values()):
        return None
    return frozenset(room.users)


def remove_invited_encryption_recipients(room: nio.MatrixRoom) -> bool:
    """Keep invitees out of nio's Megolm recipient roster."""
    if not room.encrypted:
        return False
    removed = False
    for user_id, user in tuple(room.users.items()):
        if user.invited:
            room.remove_member(user_id)
            removed = True
    return removed


def room_is_known_encrypted(
    client: nio.AsyncClient,
    room_id: str,
    room: nio.MatrixRoom | None = None,
) -> bool:
    """Return whether nio has monotonic local proof that one room is encrypted."""
    return room_id in client.encrypted_rooms or (room is not None and room.encrypted)


def room_encryption_state_from_response(response: object) -> bool | None:
    """Interpret one state-event response only when its transport succeeded."""
    if isinstance(response, nio.RoomGetStateEventResponse):
        return True if matrix_response_transport_succeeded(response) else None
    if isinstance(response, nio.RoomGetStateEventError) and matrix_response_is_not_found(response):
        return False
    return None


def mark_room_encrypted_for_delivery(client: nio.AsyncClient, room_id: str) -> bool:
    """Apply one monotonic encryption transition and fence cached delivery state."""
    room = client.rooms.get(room_id)
    if room_id in client.encrypted_rooms and (room is None or room.encrypted):
        return False
    client.encrypted_rooms.add(room_id)
    if room is not None:
        room.encrypted = True
        room.members_synced = False
    retire_outbound_group_session(client, room_id)
    return True


def _client_encryption_state(client: nio.AsyncClient) -> _ClientEncryptionState:
    return _CLIENT_ENCRYPTION_STATES.setdefault(client, _ClientEncryptionState())


def room_recipient_epoch(client: nio.AsyncClient, room_id: str) -> int:
    """Return the generation of one room's cached encryption recipients."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _CLIENT_ENCRYPTION_STATES.get(client)
        return state.recipient_epochs.get(room_id, 0) if state is not None else 0


def room_membership_epoch(client: nio.AsyncClient, room_id: str) -> int:
    """Return the generation of authoritative membership updates for one room."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _CLIENT_ENCRYPTION_STATES.get(client)
        return state.membership_epochs.get(room_id, 0) if state is not None else 0


def advance_room_membership_epoch(client: nio.AsyncClient, room_id: str) -> None:
    """Record a newer authoritative membership update for one room."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        state.membership_epochs[room_id] = state.membership_epochs.get(room_id, 0) + 1


def record_joined_members_response(client: nio.AsyncClient, response: nio.JoinedMembersResponse) -> bool:
    """Order one joined-members response and return whether it may update the cache."""
    room_id = response.room_id
    query = _ACTIVE_JOINED_MEMBERS_QUERY.get()
    matching_query = query if query is not None and query.client is client and query.room_id == room_id else None
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        current_epoch = state.membership_epochs.get(room_id, 0)
        transport_succeeded = matrix_response_transport_succeeded(response)
        response_is_current = transport_succeeded and (
            matching_query is None
            or (
                matching_query.sequence == state.joined_members_query_sequences.get(room_id, 0)
                and matching_query.start_epoch == current_epoch
            )
        )
        if matching_query is not None:
            matching_query.response = response
            matching_query.response_accepted = response_is_current
        if response_is_current:
            state.membership_epochs[room_id] = current_epoch + 1
        return response_is_current


def _joined_members_response_is_current(
    client: nio.AsyncClient,
    response: nio.JoinedMembersResponse,
    query: _JoinedMembersQuery,
) -> bool:
    """Return whether one exact query still owns its accepted response."""
    if not matrix_response_transport_succeeded(response):
        return False
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        sequence_is_current = state.joined_members_query_sequences.get(query.room_id, 0) == query.sequence
        current_epoch = state.membership_epochs.get(query.room_id, 0)
    if query.response is None:
        return sequence_is_current and current_epoch == query.start_epoch
    return (
        query.response is response
        and query.response_accepted
        and sequence_is_current
        and current_epoch == query.start_epoch + 1
    )


@contextmanager
def joined_members_query(
    client: nio.AsyncClient,
    room_id: str,
) -> Iterator[Callable[[nio.JoinedMembersResponse], bool]]:
    """Bind one joined-members request and expose its final generation check."""
    active_query = _ACTIVE_JOINED_MEMBERS_QUERY.get()
    if active_query is not None and active_query.client is client and active_query.room_id == room_id:
        yield lambda response: _joined_members_response_is_current(client, response, active_query)
        return
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        sequence = state.joined_members_query_sequences.get(room_id, 0) + 1
        state.joined_members_query_sequences[room_id] = sequence
        query = _JoinedMembersQuery(
            client=client,
            room_id=room_id,
            sequence=sequence,
            start_epoch=state.membership_epochs.get(room_id, 0),
        )
    token = _ACTIVE_JOINED_MEMBERS_QUERY.set(query)
    try:
        yield lambda response: _joined_members_response_is_current(client, response, query)
    finally:
        _ACTIVE_JOINED_MEMBERS_QUERY.reset(token)


def device_key_epoch(client: nio.AsyncClient) -> int:
    """Return the generation of device-list invalidations seen by one client."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _CLIENT_ENCRYPTION_STATES.get(client)
        return state.device_key_epoch if state is not None else 0


def advance_device_key_epoch(client: nio.AsyncClient) -> None:
    """Record a newer device-list invalidation for one client."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        _client_encryption_state(client).device_key_epoch += 1


@contextmanager
def key_query_request(
    client: nio.AsyncClient,
    user_ids: frozenset[str],
) -> Iterator[None]:
    """Bind one globally ordered device-key query to its response handling."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        state.key_query_sequence += 1
        request = _KeyQueryRequest(
            client=client,
            sequence=state.key_query_sequence,
            device_key_epoch=state.device_key_epoch,
            user_ids=user_ids,
        )
    token = _ACTIVE_KEY_QUERY_REQUEST.set(request)
    try:
        yield
    finally:
        _ACTIVE_KEY_QUERY_REQUEST.reset(token)


def key_query_response_is_current(client: nio.AsyncClient) -> bool:
    """Return whether the active key response may update nio's device store."""
    request = _ACTIVE_KEY_QUERY_REQUEST.get()
    if request is None:
        return True
    if request.client is not client:
        return False
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        response_is_current = (
            request.sequence == state.key_query_sequence and request.device_key_epoch == state.device_key_epoch
        )
        requeue_user_ids = not response_is_current and state.applied_key_query_sequence < state.key_query_sequence
    if requeue_user_ids and client.olm is not None:
        client.olm.add_changed_users(set(request.user_ids))
    return response_is_current


def record_key_query_response_applied(client: nio.AsyncClient) -> None:
    """Record the newest key-query response applied to nio's device store."""
    request = _ACTIVE_KEY_QUERY_REQUEST.get()
    if request is None or request.client is not client:
        return
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        state.applied_key_query_sequence = max(state.applied_key_query_sequence, request.sequence)


@contextmanager
def room_delivery_guard(
    client: nio.AsyncClient,
    room_id: str,
) -> Iterator[None]:
    """Bind the exact cached room state for one transport delivery."""
    room = client.rooms.get(room_id)
    encrypted = room_is_known_encrypted(client, room_id, room)
    recipient_user_ids = joined_only_recipient_user_ids(room) if room is not None and encrypted else None
    guard = _RoomDeliveryGuard(
        client=client,
        room_id=room_id,
        room=room,
        encrypted=encrypted,
        recipient_user_ids=recipient_user_ids,
        recipient_epoch=room_recipient_epoch(client, room_id),
        membership_epoch=room_membership_epoch(client, room_id),
    )
    token = _ACTIVE_ROOM_DELIVERY_GUARD.set(guard)
    try:
        yield
    finally:
        _ACTIVE_ROOM_DELIVERY_GUARD.reset(token)


def room_delivery_guard_is_current(client: nio.AsyncClient) -> bool:
    """Return whether the active transport retains its exact cached room state."""
    guard = _ACTIVE_ROOM_DELIVERY_GUARD.get()
    if guard is None:
        return True
    if guard.client is not client:
        return False
    room = client.rooms.get(guard.room_id)
    if room is not guard.room:
        return False
    if room is None:
        return (
            not guard.encrypted
            and not room_is_known_encrypted(client, guard.room_id)
            and room_recipient_epoch(client, guard.room_id) == guard.recipient_epoch
            and room_membership_epoch(client, guard.room_id) == guard.membership_epoch
        )
    if not guard.encrypted:
        return (
            not room_is_known_encrypted(client, guard.room_id, room)
            and room_recipient_epoch(client, guard.room_id) == guard.recipient_epoch
            and room_membership_epoch(client, guard.room_id) == guard.membership_epoch
        )
    recipient_user_ids = joined_only_recipient_user_ids(room) if room is not None and room.encrypted else None
    return (
        room.encrypted
        and room.members_synced
        and recipient_user_ids == guard.recipient_user_ids
        and client.olm is not None
        and guard.recipient_user_ids is not None
        and not guard.recipient_user_ids.intersection(client.users_for_key_query)
        and room_recipient_epoch(client, guard.room_id) == guard.recipient_epoch
        and room_membership_epoch(client, guard.room_id) == guard.membership_epoch
    )


def retire_outbound_group_session(client: nio.AsyncClient, room_id: str) -> bool:
    """Discard any fully or partially distributed outbound room session."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _client_encryption_state(client)
        state.recipient_epochs[room_id] = state.recipient_epochs.get(room_id, 0) + 1
        if client.olm is None:
            return False
        if room_id in client.sharing_session:
            state.deferred_session_retirements.add(room_id)
            return False
        state.deferred_session_retirements.discard(room_id)
    return client.olm.outbound_group_sessions.pop(room_id, None) is not None


def complete_deferred_outbound_group_session_retirement(
    client: nio.AsyncClient,
    room_id: str,
) -> bool:
    """Retire a session invalidated while nio was actively sharing it."""
    with _CLIENT_ENCRYPTION_STATES_GUARD:
        state = _CLIENT_ENCRYPTION_STATES.get(client)
        if state is None or room_id not in state.deferred_session_retirements:
            return False
        state.deferred_session_retirements.remove(room_id)
    if client.olm is not None:
        client.olm.outbound_group_sessions.pop(room_id, None)
    return True


__all__ = [
    "advance_device_key_epoch",
    "advance_room_membership_epoch",
    "apply_authoritative_joined_roster",
    "complete_deferred_outbound_group_session_retirement",
    "device_key_epoch",
    "joined_members_query",
    "joined_only_recipient_user_ids",
    "key_query_request",
    "key_query_response_is_current",
    "mark_room_encrypted_for_delivery",
    "record_joined_members_response",
    "record_key_query_response_applied",
    "remove_invited_encryption_recipients",
    "retire_outbound_group_session",
    "room_delivery_guard",
    "room_delivery_guard_is_current",
    "room_encryption_state_from_response",
    "room_is_known_encrypted",
    "room_membership_epoch",
    "room_recipient_epoch",
]
