"""Matrix delivery helpers for sends, edits, and attachments."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import nio
from nio import crypto
from nio.api import Api
from nio.exceptions import OlmTrustError

from mindroom.logging_config import get_logger
from mindroom.matrix.delivery_lock import room_delivery_lock
from mindroom.matrix.encryption_recipients import (
    apply_authoritative_joined_roster,
    device_key_epoch,
    joined_members_query,
    joined_only_recipient_user_ids,
    mark_room_encrypted_for_delivery,
    retire_outbound_group_session,
    room_delivery_guard,
    room_encryption_state_from_response,
    room_is_known_encrypted,
    room_membership_epoch,
)
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.media import upload_content_uri, upload_media_bytes
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.timing import emit_timing_event

if TYPE_CHECKING:
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment

logger = get_logger(__name__)

_MATRIX_TRUST_DELIVERY_ERROR_MESSAGE = "Matrix encrypted delivery rejected by local device trust policy."
_MATRIX_GENERIC_DELIVERY_ERROR_MESSAGE = "Matrix delivery raised an unexpected local exception."
# Allow one default nio recovery pump, while keeping terminal delivery bounded.
_SYNC_RECOVERY_RETRY_TIMEOUT_SECONDS = 30.0
_SYNC_RECOVERY_RETRY_INITIAL_DELAY_SECONDS = 0.05
_SYNC_RECOVERY_RETRY_MAX_DELAY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class DeliveredMatrixEvent:
    """One successfully delivered Matrix event plus the exact sent content payload."""

    event_id: str
    content_sent: dict[str, Any]


def _sanitized_delivery_error_message(error: Exception) -> str:
    """Return a log-safe Matrix delivery failure message."""
    if isinstance(error, OlmTrustError):
        return _MATRIX_TRUST_DELIVERY_ERROR_MESSAGE
    return _MATRIX_GENERIC_DELIVERY_ERROR_MESSAGE


def _log_matrix_delivery_exception(
    error: Exception,
    *,
    room_id: str,
    operation: str,
    cache_bypass: bool,
) -> None:
    """Log one local Matrix send/edit exception without exposing device details."""
    logger.error(
        "matrix_message_delivery_exception",
        room_id=room_id,
        operation=operation,
        cache_bypass=cache_bypass,
        exception_type=error.__class__.__name__,
        error_message=_sanitized_delivery_error_message(error),
    )


async def _retry_prepared_room_message_after_sync_recovery(
    send_once: Callable[[], Awaitable[object | None]],
    *,
    original_error: nio.SendRetryError,
    room_id: str,
    operation: str,
    cache_bypass: bool,
) -> object | None:
    """Retry one frozen payload within a bounded sync-recovery window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SYNC_RECOVERY_RETRY_TIMEOUT_SECONDS
    delay = _SYNC_RECOVERY_RETRY_INITIAL_DELAY_SECONDS
    first_retry = True
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise original_error
        retry_delay = min(delay, remaining)
        log = logger.warning if first_retry else logger.debug
        log(
            "Waiting to retry Matrix delivery after sync recovery",
            room_id=room_id,
            operation=operation,
            retry_in_seconds=retry_delay,
        )
        await asyncio.sleep(retry_delay)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise original_error
        try:
            return await asyncio.wait_for(send_once(), remaining)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise original_error from None
        except nio.SendRetryError:
            first_retry = False
            delay = min(delay * 2, _SYNC_RECOVERY_RETRY_MAX_DELAY_SECONDS)
        except Exception as error:
            _log_matrix_delivery_exception(
                error,
                room_id=room_id,
                operation=operation,
                cache_bypass=cache_bypass,
            )
            return None


async def _send_prepared_room_message(
    client: nio.AsyncClient,
    room_id: str,
    content_sent: dict[str, Any],
    *,
    message_type: str,
    cache_bypass: bool,
    operation: str,
    propagate_send_retry_error: bool,
    transaction_id: str | None,
) -> object | None:
    """Send one prepared Matrix room message and normalize local delivery exceptions."""

    async def send_once() -> object | None:
        if cache_bypass:
            access_token = client.access_token
            if not access_token:
                _log_matrix_delivery_exception(
                    nio.LocalProtocolError("Matrix client access token is required to send a message."),
                    room_id=room_id,
                    operation=operation,
                    cache_bypass=cache_bypass,
                )
                return None
            method, path, data = Api.room_send(
                access_token,
                room_id,
                message_type,
                content_sent,
                transaction_id or uuid4(),
            )
            return await client._send(
                nio.RoomSendResponse,
                method,
                path,
                data,
                response_data=(room_id,),
            )
        # Bots have no interactive device-verification flow, so encrypted sends
        # always deliver to unverified devices.
        if transaction_id is None:
            return await client.room_send(
                room_id=room_id,
                message_type=message_type,
                content=content_sent,
                ignore_unverified_devices=True,
            )
        return await client.room_send(
            room_id=room_id,
            message_type=message_type,
            content=content_sent,
            tx_id=transaction_id,
            ignore_unverified_devices=True,
        )

    try:
        with room_delivery_guard(client, room_id):
            return await send_once()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if propagate_send_retry_error and isinstance(error, nio.SendRetryError):
            raise
        _log_matrix_delivery_exception(
            error,
            room_id=room_id,
            operation=operation,
            cache_bypass=cache_bypass,
        )
        return None


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one room from nio's in-memory room cache if present."""
    return _cached_rooms(client).get(room_id)


async def _authoritative_cached_room_encryption(
    client: nio.AsyncClient,
    room_id: str,
    room: nio.MatrixRoom,
) -> bool | None:
    """Refresh a cached room whose apparent plaintext state can become encrypted."""
    if room_is_known_encrypted(client, room_id, room):
        mark_room_encrypted_for_delivery(client, room_id)
        return True
    encrypted = await _remote_room_encrypted(client, room_id)
    current_room = cached_room(client, room_id)
    if current_room is not room:
        return None
    if room_is_known_encrypted(client, room_id, room):
        mark_room_encrypted_for_delivery(client, room_id)
        return True
    if encrypted is not True:
        return encrypted
    mark_room_encrypted_for_delivery(client, room_id)
    return True


async def _remote_room_encrypted(client: nio.AsyncClient, room_id: str) -> bool | None:
    """Return authoritative room encryption state when readable."""
    response = await client.room_get_state_event(room_id, "m.room.encryption")
    return room_encryption_state_from_response(response)


def _room_from_remote_state(
    client: nio.AsyncClient,
    room_id: str,
    members: nio.JoinedMembersResponse,
    state: nio.RoomGetStateResponse,
) -> nio.MatrixRoom | None:
    """Return one unpublished room candidate hydrated from authoritative state."""
    room = nio.MatrixRoom(room_id=room_id, own_user_id=client.user_id or "")
    for event_source in state.events:
        event = nio.Event.parse_event(event_source)
        if isinstance(event, nio.RoomMemberEvent):
            room.handle_membership(event)
        else:
            room.handle_event(event)
    state_joined_user_ids = frozenset(user_id for user_id, user in room.users.items() if not user.invited)
    response_joined_user_ids = frozenset(member.user_id for member in members.members)
    if state_joined_user_ids != response_joined_user_ids:
        return None
    apply_authoritative_joined_roster(room, members)
    return room


@dataclass(frozen=True, slots=True)
class RoomDeliveryHydrationProof:
    """Immutable room state that must still hold at an exact delivery boundary."""

    encrypted: bool
    joined_user_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _EncryptedRoomHydration:
    """One validated room selected for encrypted delivery hydration."""

    room: nio.MatrixRoom
    joined_user_ids: frozenset[str]
    unpublished_candidate: bool


@dataclass(frozen=True, slots=True)
class _JoinedMembersHydrationSnapshot:
    """One joined-members response retained with its accepted generation."""

    response: nio.JoinedMembersResponse
    membership_epoch: int


def _joined_room_user_ids(room: nio.MatrixRoom) -> frozenset[str]:
    """Return the exact nio roster used for encrypted session sharing."""
    return frozenset(room.users)


def _retire_membership_changed_outbound_session(
    client: nio.AsyncClient,
    room_id: str,
    *,
    prior_recipient_user_ids: frozenset[str] | None,
    joined_user_ids: frozenset[str],
) -> None:
    """Discard every session that may have been exposed to obsolete members."""
    if client.olm is None or prior_recipient_user_ids == joined_user_ids:
        return
    if retire_outbound_group_session(client, room_id):
        logger.info(
            "matrix_outbound_group_session_retired_for_membership_change",
            room_id=room_id,
            prior_member_count=(len(prior_recipient_user_ids) if prior_recipient_user_ids is not None else None),
            joined_member_count=len(joined_user_ids),
        )


def _room_covers_joined_members(
    room: nio.MatrixRoom,
    joined_user_ids: frozenset[str],
) -> bool:
    """Return whether one encrypted sync room has the authoritative joined members."""
    return (
        room.encrypted
        and room.members_synced
        and not room.invited_users
        and joined_user_ids == _joined_room_user_ids(room)
    )


def _fence_incomplete_encrypted_room(room: nio.MatrixRoom) -> None:
    """Force nio sends to refresh an encrypted room rejected by hydration."""
    if room.encrypted:
        room.members_synced = False


async def _encrypted_room_for_hydration(
    client: nio.AsyncClient,
    room_id: str,
    members: nio.JoinedMembersResponse,
    joined_user_ids: frozenset[str],
    membership_epoch: int,
) -> _EncryptedRoomHydration | None:
    """Return a complete candidate or an authoritative encrypted sync room."""
    room = cached_room(client, room_id)
    if room is not None:
        apply_authoritative_joined_roster(room, members)
        if not _room_covers_joined_members(room, joined_user_ids):
            _fence_incomplete_encrypted_room(room)
            return None
        return _EncryptedRoomHydration(room, joined_user_ids, unpublished_candidate=False)

    state = await client.room_get_state(room_id)
    if room_membership_epoch(client, room_id) != membership_epoch:
        _fence_superseded_joined_members(client, room_id)
        return None
    if not isinstance(state, nio.RoomGetStateResponse):
        return None
    candidate = _room_from_remote_state(client, room_id, members, state)
    room = cached_room(client, room_id)
    if candidate is None:
        hydration = None
    elif room is None:
        hydration = (
            _EncryptedRoomHydration(candidate, joined_user_ids, unpublished_candidate=True)
            if _room_covers_joined_members(candidate, joined_user_ids)
            else None
        )
    elif not _room_covers_joined_members(room, joined_user_ids):
        _fence_incomplete_encrypted_room(room)
        hydration = None
    else:
        hydration = _EncryptedRoomHydration(room, joined_user_ids, unpublished_candidate=False)
    return hydration


def _pending_room_key_query_user_ids(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
) -> set[str]:
    """Return room members whose device keys still need a successful query."""
    if client.olm is None:
        return set()
    return set(room.users).intersection(client.users_for_key_query)


def _log_incomplete_room_device_keys(room_id: str, pending_room_members: set[str]) -> None:
    """Log an encrypted room that must remain fenced from sending."""
    logger.error(
        "matrix_encrypted_room_device_keys_incomplete",
        room_id=room_id,
        pending_member_count=len(pending_room_members),
    )


async def _room_device_keys_are_ready(
    client: nio.AsyncClient,
    room_id: str,
    room: nio.MatrixRoom,
) -> bool:
    """Query pending room keys while keeping nio's send path fenced."""
    if not client.should_query_keys:
        return True
    queried_user_ids = frozenset(client.users_for_key_query)
    prior_device_key_epoch = device_key_epoch(client)
    prior_membership_epoch = room_membership_epoch(client, room_id)
    room.members_synced = False
    key_query = await client.keys_query()
    generations_are_current = (
        device_key_epoch(client) == prior_device_key_epoch
        and room_membership_epoch(client, room_id) == prior_membership_epoch
    )
    if not generations_are_current:
        current_room = cached_room(client, room_id)
        current_recipient_user_ids = (
            set(current_room.users) if current_room is not None and current_room.encrypted else set()
        )
        if client.olm is not None:
            client.olm.add_changed_users(set(queried_user_ids) | current_recipient_user_ids)
        if current_room is not None:
            _fence_incomplete_encrypted_room(current_room)
        retire_outbound_group_session(client, room_id)
        logger.warning(
            "matrix_encrypted_room_key_query_superseded",
            room_id=room_id,
        )
        return False
    if not isinstance(key_query, nio.KeysQueryResponse):
        return False
    pending_room_members = _pending_room_key_query_user_ids(client, room)
    if pending_room_members:
        _log_incomplete_room_device_keys(room_id, pending_room_members)
        return False
    room.members_synced = True
    return True


def delivery_hydration_is_current(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
) -> bool:
    """Return whether the current cache still satisfies one hydration proof."""
    room = cached_room(client, room_id)
    if not proof.encrypted:
        return not room_is_known_encrypted(client, room_id, room)
    return (
        room is not None
        and room.encrypted
        and room.members_synced
        and not room.invited_users
        and proof.joined_user_ids == _joined_room_user_ids(room)
        and not _pending_room_key_query_user_ids(client, room)
    )


def _current_encrypted_room_after_hydration(
    client: nio.AsyncClient,
    room_id: str,
    hydration: _EncryptedRoomHydration,
) -> nio.MatrixRoom | None:
    """Adopt a concurrent sync room only when it covers the hydrated membership."""
    current_room = cached_room(client, room_id)
    if current_room is None:
        return hydration.room if hydration.unpublished_candidate else None
    if not _room_covers_joined_members(current_room, hydration.joined_user_ids):
        _retire_membership_changed_outbound_session(
            client,
            room_id,
            prior_recipient_user_ids=joined_only_recipient_user_ids(current_room),
            joined_user_ids=hydration.joined_user_ids,
        )
        _fence_incomplete_encrypted_room(current_room)
        logger.error(
            "matrix_encrypted_room_cache_changed_during_hydration",
            room_id=room_id,
            current_room_encrypted=current_room.encrypted,
            current_room_members_synced=current_room.members_synced,
        )
        return None
    if client.olm is not None:
        client.olm.update_tracked_users(current_room)
    return current_room


async def _joined_members_for_hydration(
    client: nio.AsyncClient,
    room_id: str,
    expected_room: nio.MatrixRoom | None,
) -> _JoinedMembersHydrationSnapshot | None:
    """Return a joined roster only when no newer membership state superseded it."""
    with joined_members_query(client, room_id) as query_is_current:
        members = await client.joined_members(room_id)
    if (
        isinstance(members, nio.JoinedMembersResponse)
        and cached_room(client, room_id) is expected_room
        and query_is_current()
    ):
        return _JoinedMembersHydrationSnapshot(
            response=members,
            membership_epoch=room_membership_epoch(client, room_id),
        )

    _fence_superseded_joined_members(client, room_id)
    return None


def _fence_superseded_joined_members(client: nio.AsyncClient, room_id: str) -> None:
    """Reject hydration derived from a superseded joined-members snapshot."""
    current_room = cached_room(client, room_id)
    if current_room is not None:
        _fence_incomplete_encrypted_room(current_room)
    retire_outbound_group_session(client, room_id)
    logger.warning(
        "matrix_encrypted_room_joined_members_superseded",
        room_id=room_id,
    )


async def _hydrate_encrypted_joined_room(
    client: nio.AsyncClient,
    room_id: str,
) -> RoomDeliveryHydrationProof | None:
    """Hydrate encrypted send state before publishing an owned room candidate."""
    prior_room = cached_room(client, room_id)
    prior_recipient_user_ids = (
        joined_only_recipient_user_ids(prior_room)
        if (prior_room is not None and prior_room.encrypted and prior_room.members_synced)
        else None
    )
    if prior_room is not None:
        _fence_incomplete_encrypted_room(prior_room)
    members_snapshot = await _joined_members_for_hydration(client, room_id, prior_room)
    if members_snapshot is None:
        return None
    members = members_snapshot.response
    joined_user_ids = frozenset(member.user_id for member in members.members)
    _retire_membership_changed_outbound_session(
        client,
        room_id,
        prior_recipient_user_ids=prior_recipient_user_ids,
        joined_user_ids=joined_user_ids,
    )

    hydration = await _encrypted_room_for_hydration(
        client,
        room_id,
        members,
        joined_user_ids,
        members_snapshot.membership_epoch,
    )
    if hydration is None:
        return None
    room = hydration.room
    if client.olm is not None:
        client.olm.update_tracked_users(room)
    if not await _room_device_keys_are_ready(client, room_id, room):
        return None

    room = _current_encrypted_room_after_hydration(client, room_id, hydration)
    if room is None:
        return None

    pending_room_members = _pending_room_key_query_user_ids(client, room)
    if pending_room_members:
        room.members_synced = False
        _log_incomplete_room_device_keys(room_id, pending_room_members)
        return None

    if client.store is not None:
        client.store.save_encrypted_rooms({room_id})
    client.encrypted_rooms.add(room_id)
    client.rooms.setdefault(room_id, room)
    return RoomDeliveryHydrationProof(
        encrypted=True,
        joined_user_ids=hydration.joined_user_ids,
    )


async def _hydrate_joined_room_for_delivery_locked(
    client: nio.AsyncClient,
    room_id: str,
) -> RoomDeliveryHydrationProof | None:
    """Seed nio's delivery state while holding its application send lock."""
    room = cached_room(client, room_id)
    if room is not None:
        encrypted = await _authoritative_cached_room_encryption(client, room_id, room)
        if encrypted is None:
            logger.error(
                "matrix_room_delivery_cache_hydration_failed",
                room_id=room_id,
                hint="Unable to determine room encryption state before delivery.",
            )
            return None
        if not encrypted:
            return RoomDeliveryHydrationProof(encrypted=False)
        return await _hydrate_encrypted_joined_room(client, room_id)

    encrypted = True if room_is_known_encrypted(client, room_id) else await _remote_room_encrypted(client, room_id)
    if encrypted is None:
        logger.error(
            "matrix_room_delivery_cache_hydration_failed",
            room_id=room_id,
            hint="Unable to determine room encryption state before delivery.",
        )
        return None

    if not encrypted:
        return RoomDeliveryHydrationProof(encrypted=False)

    return await _hydrate_encrypted_joined_room(client, room_id)


async def hydrate_joined_room_for_delivery(
    client: nio.AsyncClient,
    room_id: str,
) -> RoomDeliveryHydrationProof | None:
    """Seed nio's delivery state while excluding concurrent application sends."""
    async with room_delivery_lock(client, room_id):
        return await _hydrate_joined_room_for_delivery_locked(client, room_id)


def _cached_rooms(client: nio.AsyncClient) -> Mapping[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it."""
    rooms = client.rooms
    return rooms if isinstance(rooms, Mapping) else {}


def _can_send_to_encrypted_room(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool:
    """Return whether one outbound room operation can proceed with current nio E2EE support."""
    room = cached_room(client, room_id)
    if not room_is_known_encrypted(client, room_id, room) or crypto.ENCRYPTION_ENABLED:
        return True
    logger.error(
        "matrix_e2ee_support_required",
        room_id=room_id,
        operation=operation,
        hint="Reinstall MindRoom dependencies so `mindroom-nio[e2e]` is available for encrypted Matrix rooms.",
    )
    return False


async def _cache_bypass_has_plaintext_room(
    client: nio.AsyncClient,
    room_id: str,
    *,
    operation: str,
) -> bool:
    """Return whether an uncached target is authoritatively plaintext."""
    if room_is_known_encrypted(client, room_id):
        encrypted = True
    else:
        encrypted = await _remote_room_encrypted(client, room_id)
    room = cached_room(client, room_id)
    if room_is_known_encrypted(client, room_id, room):
        encrypted = True
    if encrypted is True:
        logger.error(
            "matrix_encrypted_room_send_requires_synced_room_cache",
            room_id=room_id,
            operation=operation,
            hint="Wait for initial sync to populate nio's room cache before sending to encrypted rooms.",
        )
        return False
    if encrypted is None:
        logger.error(
            "matrix_room_send_requires_known_encryption_state",
            room_id=room_id,
            operation=operation,
            hint="Unable to determine whether the room is encrypted while nio's room cache is empty.",
        )
        return False
    return True


async def _delivery_hydration_is_current_before_preparation(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
) -> bool:
    """Validate one proof before message preparation can cause side effects."""
    if proof.encrypted:
        return delivery_hydration_is_current(client, room_id, proof)
    room = cached_room(client, room_id)
    if room_is_known_encrypted(client, room_id, room):
        return False
    encrypted = (
        await _authoritative_cached_room_encryption(client, room_id, room)
        if room is not None
        else await _remote_room_encrypted(client, room_id)
    )
    if encrypted is not False:
        return False
    return delivery_hydration_is_current(client, room_id, proof)


async def _refresh_delivery_hydration_at_send(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
) -> RoomDeliveryHydrationProof | None:
    """Refresh authoritative delivery state while holding the room send lock."""
    if not proof.encrypted:
        return proof if await _delivery_hydration_is_current_before_preparation(client, room_id, proof) else None
    return await _hydrate_joined_room_for_delivery_locked(client, room_id)


def _log_stale_delivery_hydration(
    room_id: str,
    *,
    operation: str,
    proof: RoomDeliveryHydrationProof,
) -> None:
    """Log rejection of one stale proof-bound delivery."""
    logger.warning(
        "matrix_room_delivery_hydration_stale",
        room_id=room_id,
        operation=operation,
        expected_encrypted=proof.encrypted,
    )


async def _can_prepare_room_message(
    client: nio.AsyncClient,
    room_id: str,
    *,
    cache_bypass: bool,
    operation: str,
    delivery_proof: RoomDeliveryHydrationProof | None,
) -> bool:
    """Return whether message preparation can safely begin."""
    if delivery_proof is None:
        return not cache_bypass or await _cache_bypass_has_plaintext_room(
            client,
            room_id,
            operation=operation,
        )
    if await _delivery_hydration_is_current_before_preparation(client, room_id, delivery_proof):
        return True
    _log_stale_delivery_hydration(
        room_id,
        operation=operation,
        proof=delivery_proof,
    )
    return False


async def _ensure_room_delivery_ready_locked(
    client: nio.AsyncClient,
    room_id: str,
    *,
    operation: str,
) -> RoomDeliveryHydrationProof | None:
    """Return current delivery readiness while holding the room send lock."""
    if not _can_send_to_encrypted_room(client, room_id, operation=operation):
        return None
    room = cached_room(client, room_id)
    if room is None:
        if room_is_known_encrypted(client, room_id):
            return await _hydrate_encrypted_joined_room(client, room_id)
        plaintext = await _cache_bypass_has_plaintext_room(client, room_id, operation=operation)
        return RoomDeliveryHydrationProof(encrypted=False) if plaintext else None
    return await _cached_room_delivery_proof(client, room_id, room, operation=operation)


async def _cached_room_delivery_proof(
    client: nio.AsyncClient,
    room_id: str,
    room: nio.MatrixRoom,
    *,
    operation: str,
) -> RoomDeliveryHydrationProof | None:
    """Return authoritative readiness for one cached room."""
    encrypted = await _authoritative_cached_room_encryption(client, room_id, room)
    if encrypted is None:
        logger.error(
            "matrix_room_send_requires_known_encryption_state",
            room_id=room_id,
            operation=operation,
            hint="Unable to confirm cached room encryption state before sending.",
        )
        return None
    if not encrypted:
        return RoomDeliveryHydrationProof(encrypted=False)
    if not _can_send_to_encrypted_room(client, room_id, operation=operation):
        return None
    proof = RoomDeliveryHydrationProof(
        encrypted=True,
        joined_user_ids=_joined_room_user_ids(room),
    )
    if delivery_hydration_is_current(client, room_id, proof):
        return proof
    return await _hydrate_joined_room_for_delivery_locked(client, room_id)


async def ensure_room_delivery_ready(
    client: nio.AsyncClient,
    room_id: str,
    *,
    operation: str,
) -> RoomDeliveryHydrationProof | None:
    """Return an authoritative proof for state-dependent payload preparation."""
    async with room_delivery_lock(client, room_id):
        return await _ensure_room_delivery_ready_locked(client, room_id, operation=operation)


async def _prepared_delivery_is_ready_locked(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
    *,
    operation: str,
) -> bool:
    """Return whether a prepared payload retained its room encryption mode."""
    refreshed = await _refresh_delivery_hydration_at_send(client, room_id, proof)
    if refreshed is not None and refreshed.encrypted == proof.encrypted:
        return True
    _log_stale_delivery_hydration(room_id, operation=operation, proof=proof)
    return False


async def send_room_event_result(
    client: nio.AsyncClient,
    room_id: str,
    message_type: str,
    content: dict[str, Any],
    *,
    operation: str = "send_room_event",
    transaction_id: str | None = None,
    delivery_proof: RoomDeliveryHydrationProof | None = None,
) -> nio.RoomSendResponse | nio.RoomSendError | None:
    """Send one raw room event through the application-owned delivery gate."""
    proof_was_supplied = delivery_proof is not None
    async with room_delivery_lock(client, room_id):
        if delivery_proof is None:
            delivery_proof = await _ensure_room_delivery_ready_locked(
                client,
                room_id,
                operation=operation,
            )
        if delivery_proof is None:
            return None
        if (proof_was_supplied or delivery_proof.encrypted) and (
            not _can_send_to_encrypted_room(client, room_id, operation=operation)
            or not await _prepared_delivery_is_ready_locked(
                client,
                room_id,
                delivery_proof,
                operation=operation,
            )
        ):
            return None
        cache_bypass = cached_room(client, room_id) is None
        response = await _send_prepared_room_message(
            client,
            room_id,
            content,
            message_type=message_type,
            cache_bypass=cache_bypass,
            operation=operation,
            propagate_send_retry_error=False,
            transaction_id=transaction_id,
        )
    return response if isinstance(response, (nio.RoomSendResponse, nio.RoomSendError)) else None


async def send_message_result(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    operation: str = "send_message",
    retry_sync_recovery: bool = False,
    transaction_id: str | None = None,
    delivery_proof: RoomDeliveryHydrationProof | None = None,
) -> DeliveredMatrixEvent | None:
    """Send a message to a Matrix room and return the exact delivered payload."""
    transaction_id = transaction_id if transaction_id is not None else str(uuid4())
    async with room_delivery_lock(client, room_id):
        if delivery_proof is None:
            delivery_proof = await _ensure_room_delivery_ready_locked(
                client,
                room_id,
                operation=operation,
            )
        elif not _can_send_to_encrypted_room(
            client,
            room_id,
            operation=operation,
        ) or not await _can_prepare_room_message(
            client,
            room_id,
            cache_bypass=cached_room(client, room_id) is None,
            operation=operation,
            delivery_proof=delivery_proof,
        ):
            return None
        if delivery_proof is None:
            return None

    message_type = "m.room.message"
    emit_timing_event(
        "Matrix send timing",
        phase="prepare_start",
        room_id=room_id,
        message_type=message_type,
    )
    content_sent = await prepare_large_message(
        client,
        room_id,
        content,
        room_encrypted=delivery_proof.encrypted,
    )
    emit_timing_event(
        "Matrix send timing",
        phase="prepare_finish",
        room_id=room_id,
        message_type=message_type,
    )
    cache_bypass = False
    readiness_failed = False

    async def send_once() -> object | None:
        nonlocal cache_bypass, readiness_failed
        async with room_delivery_lock(client, room_id):
            ready = await _prepared_delivery_is_ready_locked(
                client,
                room_id,
                delivery_proof,
                operation=operation,
            )
            if not ready:
                readiness_failed = True
                return None
            readiness_failed = False
            cache_bypass = cached_room(client, room_id) is None
            emit_timing_event(
                "Matrix send timing",
                phase="send_start",
                room_id=room_id,
                message_type=message_type,
                cache_bypass=cache_bypass,
            )
            return await _send_prepared_room_message(
                client,
                room_id,
                content_sent,
                message_type=message_type,
                cache_bypass=cache_bypass,
                operation=operation,
                propagate_send_retry_error=retry_sync_recovery,
                transaction_id=transaction_id,
            )

    try:
        response = await send_once()
    except nio.SendRetryError as error:
        response = await _retry_prepared_room_message_after_sync_recovery(
            send_once,
            original_error=error,
            room_id=room_id,
            operation=operation,
            cache_bypass=cache_bypass,
        )
    if readiness_failed:
        return None
    if response is None:
        emit_timing_event(
            "Matrix send timing",
            phase="send_finish",
            room_id=room_id,
            message_type=message_type,
            cache_bypass=cache_bypass,
            outcome="error",
            error="delivery_exception",
        )
        return None
    if isinstance(response, nio.RoomSendResponse):
        emit_timing_event(
            "Matrix send timing",
            phase="send_finish",
            room_id=room_id,
            message_type=message_type,
            cache_bypass=cache_bypass,
            outcome="sent",
            event_id=str(response.event_id),
        )
        logger.debug(
            "matrix_message_sent",
            room_id=room_id,
            event_id=str(response.event_id),
            cache_bypass=cache_bypass,
        )
        return DeliveredMatrixEvent(event_id=str(response.event_id), content_sent=content_sent)
    emit_timing_event(
        "Matrix send timing",
        phase="send_finish",
        room_id=room_id,
        message_type=message_type,
        cache_bypass=cache_bypass,
        outcome="error",
        error=str(response),
    )
    logger.error(
        "matrix_message_send_failed",
        room_id=room_id,
        error=str(response),
        cache_bypass=cache_bypass,
    )
    return None


def _guess_mimetype(file_path: Path) -> str:
    guessed_mimetype, _ = mimetypes.guess_type(file_path.name)
    return guessed_mimetype or "application/octet-stream"


async def _read_file_bytes(file_path: Path) -> bytes | None:
    """Read one upload payload without blocking the event loop."""
    try:
        return await asyncio.to_thread(file_path.read_bytes)
    except OSError:
        logger.exception("Failed to read file before upload", path=str(file_path))
        return None


async def _upload_file_as_mxc(
    client: nio.AsyncClient,
    file_path: Path,
    *,
    mimetype: str,
    room_encrypted: bool,
    file_bytes: bytes | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload a local file as MXC, encrypting payloads in encrypted rooms."""
    if file_bytes is None:
        file_bytes = await _read_file_bytes(file_path)
        if file_bytes is None:
            return None, None

    return await _upload_media_bytes_as_mxc(
        client,
        file_bytes,
        filename=file_path.name,
        mimetype=mimetype,
        room_encrypted=room_encrypted,
    )


async def _upload_media_bytes_as_mxc(
    client: nio.AsyncClient,
    media_bytes: bytes,
    *,
    filename: str,
    mimetype: str,
    room_encrypted: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload an in-memory Matrix media payload as MXC, encrypting for encrypted rooms."""
    info: dict[str, Any] = {"size": len(media_bytes), "mimetype": mimetype}
    upload_bytes = media_bytes
    encrypted_file_payload: dict[str, Any] | None = None
    upload_mimetype = mimetype
    upload_name = filename

    if room_encrypted:
        try:
            encrypted_bytes, encryption_keys = crypto.attachments.encrypt_attachment(media_bytes)
        except Exception:
            logger.exception("Failed to encrypt Matrix media upload", filename=filename)
            return None, None
        upload_bytes = encrypted_bytes
        upload_mimetype = "application/octet-stream"
        upload_name = f"{filename}.enc"
        encrypted_file_payload = {
            "url": "",
            "key": encryption_keys["key"],
            "iv": encryption_keys["iv"],
            "hashes": encryption_keys["hashes"],
            "v": "v2",
            "mimetype": mimetype,
            "size": len(media_bytes),
        }

    try:
        upload_response = await upload_media_bytes(
            client,
            upload_bytes,
            content_type=upload_mimetype,
            filename=upload_name,
        )
    except Exception:
        logger.exception("Failed uploading Matrix media", filename=filename)
        return None, None

    mxc_uri = upload_content_uri(upload_response)
    if mxc_uri is None:
        logger.error("Failed Matrix media upload response", filename=filename, response=str(upload_response))
        return None, None

    upload_payload: dict[str, Any] = {"info": info}
    if encrypted_file_payload is not None:
        encrypted_file_payload["url"] = mxc_uri
        upload_payload["file"] = encrypted_file_payload
    return mxc_uri, upload_payload


def _msgtype_for_mimetype(mimetype: str) -> str:
    """Return the Matrix msgtype appropriate for the given MIME type."""
    major = mimetype.split("/", 1)[0]
    if major == "image":
        return "m.image"
    if major == "video":
        return "m.video"
    if major == "audio":
        return "m.audio"
    return "m.file"


def _normalized_voice_waveform(waveform: Sequence[int] | None) -> list[int]:
    """Return a Matrix-compatible waveform payload."""
    if waveform is None:
        return [0] * 30
    return [min(1024, max(0, int(level))) for level in waveform]


def _voice_audio_details(duration_ms: int | None, waveform: Sequence[int] | None) -> dict[str, Any] | None:
    """Build Matrix voice-message audio details when duration is available."""
    if duration_ms is None:
        return None
    return {
        "duration": duration_ms,
        "waveform": _normalized_voice_waveform(waveform),
    }


def _thread_relation_content(thread_id: str | None, latest_thread_event_id: str | None) -> dict[str, Any] | None:
    """Build Matrix thread relation content for media sends."""
    if thread_id is None:
        return None
    if latest_thread_event_id is None:
        msg = "latest_thread_event_id is required for thread fallback"
        raise ValueError(msg)
    return {
        "rel_type": "m.thread",
        "event_id": thread_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": latest_thread_event_id},
    }


async def send_file_message(
    client: nio.AsyncClient,
    room_id: str,
    file_path: str | Path,
    *,
    thread_id: str | None = None,
    caption: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Upload a file and send it with the appropriate Matrix message type."""
    resolved_path = Path(file_path).expanduser().resolve()
    if not resolved_path.is_file():
        logger.error("Cannot send non-file attachment", path=str(resolved_path))
        return None
    file_bytes = await _read_file_bytes(resolved_path)
    if file_bytes is None:
        return None
    delivery_proof = await ensure_room_delivery_ready(
        client,
        room_id,
        operation="send_file_message",
    )
    if delivery_proof is None:
        return None

    mimetype = _guess_mimetype(resolved_path)
    mxc_uri, upload_payload = await _upload_file_as_mxc(
        client,
        resolved_path,
        mimetype=mimetype,
        room_encrypted=delivery_proof.encrypted,
        file_bytes=file_bytes,
    )
    if mxc_uri is None or upload_payload is None:
        return None

    info = upload_payload.get("info")
    if not isinstance(info, dict):
        info = {"size": resolved_path.stat().st_size, "mimetype": mimetype}

    msgtype = _msgtype_for_mimetype(mimetype)
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or resolved_path.name,
        "info": info,
    }
    if msgtype == "m.file":
        content["filename"] = resolved_path.name
    encrypted_file_payload = upload_payload.get("file")
    if isinstance(encrypted_file_payload, dict):
        content["file"] = encrypted_file_payload
    else:
        content["url"] = mxc_uri

    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(
        client,
        room_id,
        content,
        operation="send_file_message",
        delivery_proof=delivery_proof,
    )
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


async def send_runtime_encrypted_media_message(
    client: nio.AsyncClient,
    room_id: str,
    attachment: RuntimeEncryptedMediaAttachment,
    *,
    thread_id: str | None = None,
    caption: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Send an existing encrypted MXC object without writing or uploading plaintext bytes."""
    msgtype = _msgtype_for_mimetype(attachment.mime_type)
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or attachment.filename,
        "info": {"size": attachment.size, "mimetype": attachment.mime_type},
        "file": attachment.encrypted_file_content(),
    }
    if msgtype == "m.file":
        content["filename"] = attachment.filename
    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(
        client,
        room_id,
        content,
        operation="send_runtime_encrypted_media_message",
    )
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


async def send_audio_message(
    client: nio.AsyncClient,
    room_id: str,
    audio_bytes: bytes,
    *,
    mimetype: str,
    filename: str = "voice-message.opus",
    caption: str | None = None,
    duration_ms: int | None = None,
    waveform: Sequence[int] | None = None,
    thread_id: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Upload an in-memory audio payload and send it as a Matrix voice message."""
    delivery_proof = await ensure_room_delivery_ready(
        client,
        room_id,
        operation="send_audio_message",
    )
    if delivery_proof is None:
        return None

    mxc_uri, upload_payload = await _upload_media_bytes_as_mxc(
        client,
        audio_bytes,
        filename=filename,
        mimetype=mimetype,
        room_encrypted=delivery_proof.encrypted,
    )
    if mxc_uri is None or upload_payload is None:
        return None

    info = upload_payload.get("info")
    if not isinstance(info, dict):
        info = {"size": len(audio_bytes), "mimetype": mimetype}
    audio_details = _voice_audio_details(duration_ms, waveform)
    if audio_details is not None:
        info["duration"] = audio_details["duration"]

    content: dict[str, Any] = {
        "msgtype": "m.audio",
        "body": caption or filename,
        "info": info,
    }
    if audio_details is not None:
        content["org.matrix.msc3245.voice"] = {}
        content["org.matrix.msc1767.audio"] = audio_details
    if caption:
        content["filename"] = filename
    encrypted_file_payload = upload_payload.get("file")
    if isinstance(encrypted_file_payload, dict):
        content["file"] = encrypted_file_payload
    else:
        content["url"] = mxc_uri

    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(
        client,
        room_id,
        content,
        operation="send_audio_message",
        delivery_proof=delivery_proof,
    )
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


def build_edit_event_content(
    *,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap replacement content in one Matrix m.replace edit envelope."""
    replacement_content = dict(new_content)
    if extra_content:
        replacement_content.update(extra_content)
    edit_content = build_matrix_edit_content(event_id, replacement_content)
    edit_content.update(
        {
            # Keep the fallback event's notification semantics aligned with
            # the replacement. In-progress streams use m.notice; terminal
            # updates return to m.text.
            "msgtype": replacement_content.get("msgtype", "m.text"),
            "body": f"* {new_text}",
            "format": "org.matrix.custom.html",
            "formatted_body": new_content.get("formatted_body", new_text),
        },
    )
    if extra_content:
        edit_content.update(extra_content)
    return edit_content


async def edit_message_result(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    *,
    extra_content: dict[str, Any] | None = None,
    retry_sync_recovery: bool = False,
    delivery_proof: RoomDeliveryHydrationProof | None = None,
) -> DeliveredMatrixEvent | None:
    """Edit an existing Matrix message and return the exact delivered payload."""
    edit_content = build_edit_event_content(
        event_id=event_id,
        new_content=new_content,
        new_text=new_text,
        extra_content=extra_content,
    )

    return await send_message_result(
        client,
        room_id,
        edit_content,
        operation="edit_message",
        retry_sync_recovery=retry_sync_recovery,
        delivery_proof=delivery_proof,
    )


__all__ = [
    "DeliveredMatrixEvent",
    "RoomDeliveryHydrationProof",
    "build_edit_event_content",
    "cached_room",
    "delivery_hydration_is_current",
    "edit_message_result",
    "ensure_room_delivery_ready",
    "hydrate_joined_room_for_delivery",
    "send_audio_message",
    "send_file_message",
    "send_message_result",
    "send_room_event_result",
    "send_runtime_encrypted_media_message",
]
