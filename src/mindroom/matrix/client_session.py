"""Matrix session lifecycle helpers."""

from __future__ import annotations

import os
import ssl as ssl_module
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import nio
from nio.client.sliding_membership import sliding_room_is_invite
from nio.responses import RegisterInteractiveResponse, RoomPutAliasError

from mindroom.constants import (
    CONFIG_CONFIRMATION_REACTION_KEY,
    STREAM_STATUS_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    RuntimePaths,
    encryption_keys_dir,
    runtime_matrix_ssl_verify,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.encryption_recipients import (
    advance_device_key_epoch,
    advance_room_membership_epoch,
    apply_authoritative_joined_roster,
    complete_deferred_outbound_group_session_retirement,
    joined_members_query,
    joined_only_recipient_user_ids,
    key_query_request,
    key_query_response_is_current,
    mark_room_encrypted_for_delivery,
    record_joined_members_response,
    record_key_query_response_applied,
    remove_invited_encryption_recipients,
    retire_outbound_group_session,
    room_delivery_guard_is_current,
)
from mindroom.matrix.event_types import CALL_ENCRYPTION_KEYS_EVENT_TYPE
from mindroom.matrix.response_status import matrix_response_transport_succeeded
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, MutableSequence
    from uuid import UUID

    from aiohttp import ClientResponse
    from nio.client.async_client import _SyncResponseEnvelope
    from nio.client.sync_response_ordering import OneTimeKeyCountCommit

logger = get_logger(__name__)

_GROUP_SESSION_SHARE_ROOM_ID: ContextVar[str | None] = ContextVar(
    "mindroom_group_session_share_room_id",
    default=None,
)

_PERMANENT_MATRIX_STARTUP_ERROR_CODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_USER_DEACTIVATED",
        "M_UNKNOWN_TOKEN",
        "M_INVALID_USERNAME",
    },
)


def _log_custom_olm_rejection(
    event: nio.UnknownToDeviceEvent,
    reason: str,
    **details: object,
) -> None:
    """Log why a security-sensitive custom event failed provenance checks."""
    log_event = "call_key_olm_rejected" if event.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE else "custom_olm_rejected"
    logger.warning(
        log_event,
        sender=event.sender,
        event_type=event.type,
        reason=reason,
        **details,
    )


def _complete_encrypted_recipient_roster(room: nio.MatrixRoom) -> frozenset[str] | None:
    """Return a complete joined-only roster suitable for change detection."""
    if not room.encrypted or not room.members_synced:
        return None
    return joined_only_recipient_user_ids(room)


def _ready_encrypted_recipient_roster(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
) -> frozenset[str] | None:
    """Return the exact roster only when nio can encrypt without refreshing."""
    recipient_user_ids = _complete_encrypted_recipient_roster(room)
    if client.olm is None or recipient_user_ids is None:
        return None
    if recipient_user_ids.intersection(client.users_for_key_query):
        return None
    return recipient_user_ids


def _retire_session_after_recipient_change(
    client: nio.AsyncClient,
    room_id: str,
    *,
    prior_recipient_user_ids: frozenset[str] | None,
    room: nio.MatrixRoom,
) -> None:
    """Retire a session when a synchronous membership owner changes recipients."""
    current_recipient_user_ids = joined_only_recipient_user_ids(room) if room.encrypted else None
    if prior_recipient_user_ids == current_recipient_user_ids:
        return
    if client.olm is not None and current_recipient_user_ids is not None:
        client.olm.update_tracked_users(room)
    if retire_outbound_group_session(client, room_id):
        logger.info(
            "matrix_outbound_group_session_retired_for_membership_change",
            room_id=room_id,
            prior_member_count=(len(prior_recipient_user_ids) if prior_recipient_user_ids is not None else None),
            joined_member_count=(len(current_recipient_user_ids) if current_recipient_user_ids is not None else None),
        )


def _membership_reset_room_ids(response: nio.SyncResponse | nio.SlidingSyncResponse) -> frozenset[str]:
    """Return rooms whose response proves the client is no longer joined."""
    if isinstance(response, nio.SyncResponse):
        return frozenset(response.rooms.leave) | frozenset(response.rooms.invite)
    return frozenset(
        room_id
        for room_id, room in response.rooms.items()
        if sliding_room_is_invite(room) or room.membership in ("leave", "ban")
    )


def _is_membership_update(event: object) -> bool:
    """Return whether one sync item carries room membership state."""
    return isinstance(event, nio.RoomMemberEvent) or (
        isinstance(event, nio.SlidingSyncStateStub) and event.type == "m.room.member"
    )


def _membership_update_room_ids(response: nio.SyncResponse | nio.SlidingSyncResponse) -> frozenset[str]:
    """Return rooms with membership state that can supersede an in-flight roster query."""
    room_ids = set(_membership_reset_room_ids(response))
    if isinstance(response, nio.SyncResponse):
        room_ids.update(
            room_id
            for room_id, room in response.rooms.join.items()
            if any(_is_membership_update(event) for event in (*room.state, *room.timeline.events))
        )
    else:
        room_ids.update(
            room_id
            for room_id, room in response.rooms.items()
            if any(_is_membership_update(event) for event in (*room.required_state, *room.timeline))
        )
    return frozenset(room_ids)


def _encryption_update_room_ids(response: nio.SyncResponse | nio.SlidingSyncResponse) -> frozenset[str]:
    """Return rooms whose response proves encryption is enabled."""
    if isinstance(response, nio.SyncResponse):
        return frozenset(
            room_id
            for room_id, room in response.rooms.join.items()
            if any(isinstance(event, nio.RoomEncryptionEvent) for event in (*room.state, *room.timeline.events))
        )
    return frozenset(
        room_id
        for room_id, room in response.rooms.items()
        if any(isinstance(event, nio.RoomEncryptionEvent) for event in (*room.required_state, *room.timeline))
    )


def _joined_count_disagrees(room: nio.MatrixRoom, joined_count: int | None) -> bool:
    """Return whether a sync summary disproves the cached joined roster."""
    if joined_count is None:
        return False
    recipient_user_ids = joined_only_recipient_user_ids(room)
    return recipient_user_ids is None or len(recipient_user_ids) != joined_count


def _joined_count_mismatch_room_ids(
    client: nio.AsyncClient,
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> frozenset[str]:
    """Return cached rooms whose summary disproves their recipient roster."""
    if isinstance(response, nio.SyncResponse):
        joined_counts = (
            (room_id, info.summary.joined_member_count)
            for room_id, info in response.rooms.join.items()
            if info.summary is not None
        )
    else:
        joined_counts = ((room_id, info.joined_count) for room_id, info in response.rooms.items())
    return frozenset(
        room_id
        for room_id, joined_count in joined_counts
        if (room := client.rooms.get(room_id)) is not None and _joined_count_disagrees(room, joined_count)
    )


def _preapply_membership_invalidations(
    client: nio.AsyncClient,
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> None:
    """Fence every cached encrypted roster disproved by one sync response."""
    room_ids = _membership_update_room_ids(response) | _joined_count_mismatch_room_ids(client, response)
    for room_id in room_ids:
        advance_room_membership_epoch(client, room_id)
        room = client.rooms.get(room_id)
        if room is not None and room.encrypted:
            room.members_synced = False
            retire_outbound_group_session(client, room_id)


def _preapply_encryption_invalidations(
    client: nio.AsyncClient,
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> None:
    """Make parsed encryption state monotonic before sync processing awaits."""
    for room_id in _encryption_update_room_ids(response):
        mark_room_encrypted_for_delivery(client, room_id)


def _preapply_device_invalidations(
    client: nio.AsyncClient,
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> None:
    """Fence sessions and key queries invalidated by one device-list delta."""
    if client.olm is None:
        return
    changed_user_ids = frozenset(response.device_list.changed) | frozenset(response.device_list.left)
    if changed_user_ids:
        advance_device_key_epoch(client)
    for room_id, room in client.rooms.items():
        if room.encrypted and changed_user_ids.intersection(room.users):
            retire_outbound_group_session(client, room_id)
    if changed_user_ids:
        client.olm.add_changed_users(set(changed_user_ids))


class _JsonResponseFactory(Protocol):
    """Nio response-class surface used to construct endpoint-specific errors."""

    def from_dict(self, parsed_dict: dict[str, object], *data: object) -> nio.Response:
        """Construct a response from one Matrix JSON object."""
        ...


_SIMPLE_TRANSPORT_ERROR_CLASSES: dict[type[nio.Response], type[nio.ErrorResponse]] = {
    nio.ContentRepositoryConfigResponse: nio.ContentRepositoryConfigError,
    nio.RoomDeleteAliasResponse: nio.RoomDeleteAliasError,
    nio.RoomPutAliasResponse: RoomPutAliasError,
}


@dataclass(frozen=True)
class _MatrixErrorDetails:
    """Validated fields retained from a failed Matrix transport body."""

    message: str
    status_code: str
    retry_after_ms: int | None
    soft_logout: bool


def _matrix_error_details(body: object) -> _MatrixErrorDetails:
    """Return safe Matrix error fields, ignoring any success-shaped payload."""
    parsed_body = cast("dict[str, object]", body) if isinstance(body, dict) else {}
    raw_message = parsed_body.get("error")
    raw_status_code = parsed_body.get("errcode")
    raw_retry_after_ms = parsed_body.get("retry_after_ms")
    return _MatrixErrorDetails(
        message=raw_message if isinstance(raw_message, str) else "Matrix response transport failed.",
        status_code=raw_status_code if isinstance(raw_status_code, str) else "M_UNKNOWN",
        retry_after_ms=(
            raw_retry_after_ms
            if isinstance(raw_retry_after_ms, int) and not isinstance(raw_retry_after_ms, bool)
            else None
        ),
        soft_logout=parsed_body.get("soft_logout") is True,
    )


def _matrix_error_body(details: _MatrixErrorDetails) -> dict[str, object]:
    """Render details through nio's normal endpoint error factories."""
    body: dict[str, object] = {
        "errcode": details.status_code,
        "error": details.message,
    }
    if details.retry_after_ms is not None:
        body["retry_after_ms"] = details.retry_after_ms
    if details.soft_logout:
        body["soft_logout"] = True
    return body


def _contextual_transport_error(
    response_class: type[nio.Response],
    data: tuple[Any, ...],
    details: _MatrixErrorDetails,
) -> nio.ErrorResponse | None:
    """Return errors for nio factories that lose endpoint context."""
    error_kwargs = {
        "status_code": details.status_code,
        "retry_after_ms": details.retry_after_ms,
        "soft_logout": details.soft_logout,
    }
    if response_class is nio.RoomGetStateEventResponse:
        return nio.RoomGetStateEventError(details.message, room_id=cast("str", data[-1]), **error_kwargs)
    if response_class is nio.KeysClaimResponse:
        room_id = cast("str", data[0]) if data else ""
        return nio.KeysClaimError(details.message, room_id=room_id, **error_kwargs)
    if response_class is nio.ToDeviceResponse:
        return nio.ToDeviceError(
            details.message,
            to_device_message=cast("nio.ToDeviceMessage", data[0]),
            **error_kwargs,
        )
    if response_class is nio.ShareGroupSessionResponse:
        return nio.ShareGroupSessionError(
            details.message,
            room_id=cast("str", data[0]),
            users_shared_with=cast("set[tuple[str, str]]", data[1]),
            **error_kwargs,
        )
    if response_class is nio.RoomReadMarkersResponse:
        return nio.RoomReadMarkersError(details.message, room_id=cast("str", data[0]), **error_kwargs)
    return None


def _file_transport_error(
    response_class: type[nio.Response],
    details: _MatrixErrorDetails,
) -> nio.ErrorResponse | None:
    """Return the declared error type for a failed file response."""
    error_kwargs = {
        "status_code": details.status_code,
        "retry_after_ms": details.retry_after_ms,
        "soft_logout": details.soft_logout,
    }
    if issubclass(response_class, nio.DownloadResponse):
        return nio.DownloadError(details.message, **error_kwargs)
    if issubclass(response_class, nio.ThumbnailResponse):
        return nio.ThumbnailError(details.message, **error_kwargs)
    if issubclass(response_class, nio.FileResponse):
        return nio.DownloadError(details.message, **error_kwargs)
    return None


def _matrix_transport_error(
    response_class: type[nio.Response],
    data: tuple[Any, ...],
    details: _MatrixErrorDetails,
) -> nio.ErrorResponse:
    """Construct a failed transport through the endpoint's typed error contract."""
    error = _contextual_transport_error(response_class, data, details) or _file_transport_error(
        response_class,
        details,
    )
    if error is None:
        simple_error_class = _SIMPLE_TRANSPORT_ERROR_CLASSES.get(response_class)
        if simple_error_class is not None:
            error = simple_error_class(
                details.message,
                status_code=details.status_code,
                retry_after_ms=details.retry_after_ms,
                soft_logout=details.soft_logout,
            )
    if error is None:
        response_factory = cast(_JsonResponseFactory, response_class)  # noqa: TC006
        parsed_error = response_factory.from_dict(_matrix_error_body(details), *data)
        if not isinstance(parsed_error, nio.ErrorResponse):
            message = f"{response_class.__name__} accepted a Matrix error body as a success response."
            raise TypeError(message)
        error = parsed_error
    return error


def _retire_active_group_session_share(client: nio.AsyncClient) -> None:
    """Fence the room session owned by the current nested share request."""
    room_id = _GROUP_SESSION_SHARE_ROOM_ID.get()
    if room_id is not None:
        retire_outbound_group_session(client, room_id)


def _abort_owned_group_session_share(client: nio.AsyncClient, room_id: str) -> None:
    """Clean nio state left behind when its sharing prerequisite raises early."""
    event = client.sharing_session.pop(room_id, None)
    if event is not None:
        event.set()
    if not complete_deferred_outbound_group_session_retirement(client, room_id):
        retire_outbound_group_session(client, room_id)


class PermanentMatrixStartupError(PermanentStartupError):
    """Raised for Matrix startup failures that should not be retried."""


@runtime_checkable
class _AsyncRequestHeaders(Protocol):
    async def prepare(self) -> None:
        """Prepare dynamic headers without blocking the event loop."""
        ...


class _MindRoomAsyncClient(nio.AsyncClient):
    """Matrix client for MindRoom-specific encrypted event behavior."""

    async def create_matrix_response(
        self,
        response_class: type,
        transport_response: ClientResponse,
        data: tuple[Any, ...] | None = None,
        save_to: os.PathLike | None = None,
    ) -> nio.Response:
        """Normalize failed success payloads before callbacks or endpoint handling."""
        if transport_response.status in range(200, 300) or (
            transport_response.status == 401
            and response_class in (nio.DeleteDevicesResponse, RegisterInteractiveResponse)
        ):
            return await super().create_matrix_response(
                response_class,
                transport_response,
                data=data,
                save_to=save_to,
            )
        body = await self.parse_body(transport_response)
        error = _matrix_transport_error(
            cast("type[nio.Response]", response_class),
            data or (),
            _matrix_error_details(body),
        )
        error.transport_response = cast("Any", transport_response)
        return error

    async def _send(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record the final outcome of nested group-session requests."""
        request_completed = False
        try:
            response = await super()._send(*args, **kwargs)
            request_completed = True
        finally:
            if not request_completed:
                _retire_active_group_session_share(self)
        if isinstance(response, nio.ErrorResponse):
            _retire_active_group_session_share(self)
        return response

    async def send(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Prepare dynamic request headers before every transport attempt."""
        headers = self.config.custom_headers
        if isinstance(headers, _AsyncRequestHeaders):
            await headers.prepare()
        if not room_delivery_guard_is_current(self):
            message = "Matrix room delivery state changed before transport delivery."
            raise nio.SendRetryError(message)
        return await super().send(*args, **kwargs)

    async def keys_query(self) -> nio.KeysQueryResponse | nio.KeysQueryError:
        """Order device-key responses across concurrent runtime queries."""
        with key_query_request(self, frozenset(self.users_for_key_query)):
            response = await super().keys_query()
        if isinstance(response, nio.KeysQueryResponse) and not matrix_response_transport_succeeded(response):
            return nio.KeysQueryError("Device-key response transport failed.")
        return response

    async def joined_members(self, room_id: str) -> nio.JoinedMembersResponse | nio.JoinedMembersError:
        """Order every joined-members response before nio may publish its roster."""
        with joined_members_query(self, room_id) as response_is_current:
            response = await super().joined_members(room_id)
        if isinstance(response, nio.JoinedMembersResponse) and not response_is_current(response):
            return nio.JoinedMembersError(
                "Joined-members response was rejected as stale or transport-failed.",
                room_id=room_id,
            )
        return response

    async def share_group_session(
        self,
        room_id: str,
        ignore_unverified_devices: bool = False,
    ) -> nio.ShareGroupSessionResponse | nio.ShareGroupSessionError:
        """Fail an aggregate key share when any per-device transport failed."""
        preexisting_share_event = self.sharing_session.get(room_id)
        token = _GROUP_SESSION_SHARE_ROOM_ID.set(room_id)
        share_completed = False
        try:
            response = await super().share_group_session(
                room_id,
                ignore_unverified_devices=ignore_unverified_devices,
            )
            share_completed = True
        finally:
            _GROUP_SESSION_SHARE_ROOM_ID.reset(token)
            if not share_completed and preexisting_share_event is None:
                _abort_owned_group_session_share(self, room_id)
        if complete_deferred_outbound_group_session_retirement(self, room_id):
            message = "Encrypted room session sharing failed."
            raise nio.SendRetryError(message)
        return response

    def invalidate_outbound_session(self, room_id: str) -> None:
        """Retire every session invalidated by a membership transition."""
        room = self.rooms.get(room_id)
        if room is not None:
            remove_invited_encryption_recipients(room)
            if room.encrypted and self.olm is not None:
                self.olm.update_tracked_users(room)
        retire_outbound_group_session(self, room_id)

    def _invalidate_session_for_member_event(self, room_id: str) -> None:
        """Order every applied member event against authoritative roster reads."""
        advance_room_membership_epoch(self, room_id)
        super()._invalidate_session_for_member_event(room_id)

    def _apply_sliding_sync_summary(
        self,
        room: nio.MatrixRoom,
        sliding_room: nio.SlidingSyncRoom,
    ) -> None:
        """Keep non-authoritative Sliding heroes out of encryption recipients."""
        prior_user_ids = frozenset(room.users)
        super()._apply_sliding_sync_summary(room, sliding_room)
        if not room.encrypted:
            return
        summary_user_ids = frozenset(room.users).difference(prior_user_ids)
        for user_id in summary_user_ids:
            room.remove_member(user_id)
        removed_invitees = remove_invited_encryption_recipients(room)
        joined_count_mismatch = _joined_count_disagrees(room, sliding_room.joined_count)
        if summary_user_ids or removed_invitees or joined_count_mismatch:
            advance_room_membership_epoch(self, room.room_id)
            room.members_synced = False
            retire_outbound_group_session(self, room.room_id)

    def _handle_joined_members(self, response: nio.JoinedMembersResponse) -> None:
        """Apply joined membership without retaining encrypted-room invitees."""
        if not record_joined_members_response(self, response):
            return
        room = self.rooms.get(response.room_id)
        if room is None:
            return
        prior_recipient_user_ids = joined_only_recipient_user_ids(room) if room.encrypted else None
        apply_authoritative_joined_roster(room, response)
        if room.encrypted and self.olm is not None:
            self.olm.update_tracked_users(room)
        _retire_session_after_recipient_change(
            self,
            response.room_id,
            prior_recipient_user_ids=prior_recipient_user_ids,
            room=room,
        )

    def _handle_joined_state(
        self,
        room_id: str,
        join_info: nio.RoomInfo,
        encrypted_rooms: set[str],
    ) -> None:
        """Apply state without exposing encrypted-room invitees to later callbacks."""
        prior_room = self.rooms.get(room_id)
        prior_recipient_user_ids = (
            joined_only_recipient_user_ids(prior_room) if prior_room is not None and prior_room.encrypted else None
        )
        super()._handle_joined_state(room_id, join_info, encrypted_rooms)
        room = self.rooms[room_id]
        remove_invited_encryption_recipients(room)
        _retire_session_after_recipient_change(
            self,
            room_id,
            prior_recipient_user_ids=prior_recipient_user_ids,
            room=room,
        )
        joined_count = join_info.summary.joined_member_count if join_info.summary is not None else None
        if room.encrypted and _joined_count_disagrees(room, joined_count):
            room.members_synced = False
            retire_outbound_group_session(self, room_id)

    def _handle_timeline_event(
        self,
        event: nio.Event | nio.BadEventType,
        room_id: str,
        room: nio.MatrixRoom,
        encrypted_rooms: set[str],
    ) -> nio.Event | nio.BadEventType | None:
        """Apply one event without yielding an invite-polluted crypto roster."""
        prior_recipient_user_ids = joined_only_recipient_user_ids(room) if room.encrypted else None
        decrypted = super()._handle_timeline_event(event, room_id, room, encrypted_rooms)
        remove_invited_encryption_recipients(room)
        _retire_session_after_recipient_change(
            self,
            room_id,
            prior_recipient_user_ids=prior_recipient_user_ids,
            room=room,
        )
        return decrypted

    async def _handle_joined_rooms(self, response: nio.SyncResponse) -> None:
        """Remove encrypted-room invitees before sync response handling completes."""
        await super()._handle_joined_rooms(response)
        for room_id in response.rooms.join:
            room = self.rooms.get(room_id)
            if room is not None:
                remove_invited_encryption_recipients(room)

    async def _process_timeline(
        self,
        room_id: str,
        room: nio.MatrixRoom,
        timeline: MutableSequence[nio.Event | nio.BadEventType],
        encrypted_rooms: set[str],
        deduplicate: bool = False,
    ) -> None:
        """Fence required-state invitees before any timeline callback can yield."""
        if remove_invited_encryption_recipients(room):
            advance_room_membership_epoch(self, room_id)
            retire_outbound_group_session(self, room_id)
        if room.encrypted and self.olm is not None:
            self.olm.update_tracked_users(room)
        await super()._process_timeline(
            room_id,
            room,
            timeline,
            encrypted_rooms,
            deduplicate=deduplicate,
        )

    def _preapply_delivery_invalidations(self, response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Fence membership and device changes before any sync callback can send."""
        _preapply_membership_invalidations(self, response)
        _preapply_encryption_invalidations(self, response)
        _preapply_device_invalidations(self, response)

    async def _receive_sync_family(self, envelope: _SyncResponseEnvelope) -> None:
        """Reject failed sync transports before ordered response ingestion."""
        if not matrix_response_transport_succeeded(envelope.response):
            return
        await super()._receive_sync_family(envelope)

    async def _handle_sync(
        self,
        envelope: _SyncResponseEnvelope,
        *,
        one_time_key_count_commit: OneTimeKeyCountCommit | None = None,
    ) -> None:
        """Fence delivery from the ordered Classic Sync response before handling awaits."""
        response = envelope.response
        if isinstance(response, nio.SyncResponse) and self.next_batch != response.next_batch:
            self._preapply_delivery_invalidations(response)
        await super()._handle_sync(
            envelope,
            one_time_key_count_commit=one_time_key_count_commit,
        )

    async def _handle_sliding_sync(
        self,
        response: nio.SlidingSyncResponse,
        *,
        one_time_key_count_commit: OneTimeKeyCountCommit | None = None,
    ) -> None:
        """Fence delivery from the ordered Sliding Sync response before handling awaits."""
        self._preapply_delivery_invalidations(response)
        await super()._handle_sliding_sync(
            response,
            one_time_key_count_commit=one_time_key_count_commit,
        )

    async def receive_response(self, response: nio.Response) -> None:
        """Fence delivery invalidations before sync recovery or callbacks can await."""
        if isinstance(response, nio.KeysQueryResponse) and (
            not matrix_response_transport_succeeded(response) or not key_query_response_is_current(self)
        ):
            return
        await super().receive_response(response)
        if isinstance(response, nio.KeysQueryResponse):
            record_key_query_response_applied(self)

    async def _prepare_room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
        tx_id: str | UUID,
        ignore_unverified_devices: bool,
    ) -> tuple[str, str, str]:
        """Reject stale crypto state instead of letting nio refresh and send through it."""
        room = self.rooms.get(room_id)
        recipient_user_ids: frozenset[str] | None = None
        if room is not None and room.encrypted:
            recipient_user_ids = _ready_encrypted_recipient_roster(self, room)
            if recipient_user_ids is None:
                message = "Encrypted room delivery readiness must be refreshed before sending."
                raise nio.SendRetryError(message)
        retirement_was_deferred = False
        try:
            request = await super()._prepare_room_send(
                room_id,
                message_type,
                content,
                tx_id,
                ignore_unverified_devices,
            )
        finally:
            retirement_was_deferred = complete_deferred_outbound_group_session_retirement(
                self,
                room_id,
            )
        if retirement_was_deferred:
            message = "Encrypted room recipients changed during session sharing."
            raise nio.SendRetryError(message)
        if recipient_user_ids is None:
            return request
        current_room = self.rooms.get(room_id)
        current_recipient_user_ids = (
            _ready_encrypted_recipient_roster(self, current_room) if current_room is not None else None
        )
        if current_room is room and current_recipient_user_ids == recipient_user_ids:
            return request
        retire_outbound_group_session(self, room_id)
        message = "Encrypted room recipients changed during send preparation."
        raise nio.SendRetryError(message)

    def encrypt(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Expose coarse delivery markers needed without decrypting room history."""
        encrypted_message_type, encrypted_content = super().encrypt(room_id, message_type, content)
        stream_status = content.get(STREAM_STATUS_KEY)
        if isinstance(stream_status, str):
            encrypted_content[STREAM_STATUS_KEY] = stream_status
        if content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True:
            encrypted_content[VISIBLE_ROUTER_VOICE_ECHO_KEY] = True
        config_reaction_id = content.get(CONFIG_CONFIRMATION_REACTION_KEY)
        if isinstance(config_reaction_id, str):
            encrypted_content[CONFIG_CONFIRMATION_REACTION_KEY] = config_reaction_id
        return encrypted_message_type, encrypted_content

    def _handle_olm_events(self, response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Apply OTK counts without replaying preapplied device invalidations."""
        count = response.device_key_count.signed_curve25519
        if self.olm is not None and count is not None:
            self.olm.uploaded_key_count = count

    def _handle_decrypt_to_device(self, to_device_event: nio.ToDeviceEvent) -> nio.ToDeviceEvent | None:
        decrypted = super()._handle_decrypt_to_device(to_device_event)
        if not isinstance(to_device_event, nio.OlmEvent) or not isinstance(decrypted, nio.UnknownToDeviceEvent):
            return decrypted
        if self.olm is None:
            _log_custom_olm_rejection(decrypted, "missing_olm_machine")
            return decrypted
        matching_devices = [
            device
            for device in self.olm.device_store.active_user_devices(decrypted.sender)
            if device.curve25519 == to_device_event.sender_key
        ]
        if len(matching_devices) != 1:
            if not matching_devices:
                self.olm.users_for_key_query.add(decrypted.sender)
            _log_custom_olm_rejection(
                decrypted,
                "curve25519_device_match_count",
                matching_device_count=len(matching_devices),
                key_query_queued=not matching_devices,
            )
            return decrypted
        device = matching_devices[0]

        # The Olm envelope authenticates possession of ``sender_key`` and nio
        # verifies that the sender in the decrypted payload matches the
        # envelope sender. Matrix clients do not all include nio's optional
        # ``sender_device``/``keys`` fields in custom Olm payloads, so map the
        # authenticated curve25519 key to the uniquely matching device from
        # the signed device-key store. If redundant identity fields are
        # present, continue to enforce them as consistency checks.
        sender_device = decrypted.source.get("sender_device")
        sender_keys = decrypted.source.get("keys")
        sender_ed25519 = sender_keys.get("ed25519") if isinstance(sender_keys, dict) else None
        if sender_device is not None and sender_device != device.id:
            _log_custom_olm_rejection(
                decrypted,
                "signed_sender_identity_mismatch",
                sender_device=sender_device,
                matched_device_id=device.id,
            )
            return decrypted
        if sender_keys is not None and sender_ed25519 != device.ed25519:
            _log_custom_olm_rejection(
                decrypted,
                "signed_sender_identity_mismatch",
                sender_ed25519=sender_ed25519,
                matched_ed25519=device.ed25519,
            )
            return decrypted
        if decrypted.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE:
            logger.info(
                "call_key_olm_authenticated",
                sender=decrypted.sender,
                sender_device=device.id,
            )
        return AuthenticatedToDeviceEvent(
            source=decrypted.source,
            sender=decrypted.sender,
            type=decrypted.type,
            authenticated_device_id=device.id,
        )


def _require_runtime_paths_arg(runtime_paths: object) -> RuntimePaths:
    """Reject stale positional call shapes with a clear error."""
    if isinstance(runtime_paths, RuntimePaths):
        return runtime_paths
    msg = (
        "matrix_client() requires RuntimePaths as its second argument. "
        "Call matrix_client(homeserver, runtime_paths, user_id=...)"
    )
    raise TypeError(msg)


def matrix_startup_error(
    message: str,
    *,
    response: object | None = None,
    permanent: bool = False,
) -> ValueError:
    """Return the appropriate startup exception type for a Matrix failure."""
    if permanent:
        return PermanentMatrixStartupError(message)
    if isinstance(response, nio.ErrorResponse) and response.status_code in _PERMANENT_MATRIX_STARTUP_ERROR_CODES:
        return PermanentMatrixStartupError(message)
    return ValueError(message)


def _maybe_ssl_context(homeserver: str, runtime_paths: RuntimePaths) -> ssl_module.SSLContext | None:
    if homeserver.startswith("https://"):
        if not runtime_matrix_ssl_verify(runtime_paths=runtime_paths):
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            ssl_context = ssl_module.create_default_context()
        return ssl_context
    return None


def olm_store_dir(user_id: str, runtime_paths: RuntimePaths) -> Path:
    """Return the per-user encryption store directory."""
    safe_user_id = user_id.replace(":", "_").replace("@", "")
    return encryption_keys_dir(runtime_paths=runtime_paths) / safe_user_id


def olm_store_exists(user_id: str, device_id: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether the persisted olm store for one device is present on disk."""
    # nio's SqliteStore names its database {user_id}_{device_id}.db inside store_path.
    return (olm_store_dir(user_id, runtime_paths) / f"{user_id}_{device_id}.db").is_file()


def matrix_client_config(*, http_headers: Mapping[str, str] | None = None) -> nio.AsyncClientConfig:
    """Return nio config, copying plain headers while preserving request-time mappings."""
    custom_headers = dict(http_headers) if isinstance(http_headers, dict) else http_headers
    return nio.AsyncClientConfig(
        backfill_limited_timelines=True,
        backfill_persist_recovery=True,
        store_sync_tokens=True,
        custom_headers=cast("dict[str, str] | None", custom_headers),
        replace_rotated_device_keys=True,
    )


def _create_matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
    store_path: str | None = None,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Create a Matrix client with consistent configuration."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    ssl_context = _maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    if store_path is None and user_id:
        store_path = str(olm_store_dir(user_id, runtime_paths=runtime_paths))
        store_dir = Path(store_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            store_dir.chmod(0o700)

    client = _MindRoomAsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        # Agents trust devices on first use and never verify interactively;
        # accept a peer device's re-registered olm identity (trust reset)
        # instead of keeping stale keys that silently break E2EE and calls.
        config=matrix_client_config(http_headers=http_headers),
        ssl=ssl_context,  # ty: ignore[invalid-argument-type]
    )
    if user_id:
        client.user_id = user_id
    if access_token:
        client.access_token = access_token
    return client


def create_authenticated_client(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Create a Matrix client from newly issued login credentials."""
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
    )
    client.restore_login(user_id, device_id, access_token)
    return client


@asynccontextmanager
async def matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
) -> AsyncGenerator[nio.AsyncClient, None]:
    """Context manager for Matrix client that ensures proper cleanup."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
    try:
        yield client
    finally:
        await client.close()


async def login(
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Login to Matrix and return an authenticated client."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, http_headers=http_headers)

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        client.user_id = response.user_id
        client.device_id = response.device_id
        client.access_token = response.access_token
        logger.info("matrix_login_succeeded", user_id=response.user_id)
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


async def login_with_token(
    homeserver: str,
    login_token: str,
    runtime_paths: RuntimePaths,
    *,
    expected_user_id: str | None = None,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Exchange one short-lived Matrix login token and restore its exact device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    login_client = _create_matrix_client(homeserver, runtime_paths, http_headers=http_headers)
    try:
        response = await login_client.login(
            token=login_token,
            device_name="MindRoom Desktop Bridge",
        )
        if not isinstance(response, nio.LoginResponse):
            msg = f"Failed to exchange Matrix login token: {response}"
            raise matrix_startup_error(msg, response=response)
        if expected_user_id is not None and response.user_id != expected_user_id:
            await _revoke_unexpected_login(
                login_client,
                expected_user_id=expected_user_id,
                actual_user_id=response.user_id,
            )
            msg = f"Matrix SSO returned {response.user_id}, but {expected_user_id} was requested."
            raise matrix_startup_error(msg, permanent=True)
        credentials = (response.user_id, response.device_id, response.access_token)
    finally:
        await login_client.close()

    user_id, device_id, access_token = credentials
    logger.info("matrix_login_succeeded", user_id=user_id, login_method="token")
    return create_authenticated_client(
        homeserver,
        user_id,
        device_id,
        access_token,
        runtime_paths,
        http_headers=http_headers,
    )


async def _revoke_unexpected_login(
    client: nio.AsyncClient,
    *,
    expected_user_id: str,
    actual_user_id: str,
) -> None:
    """Best-effort revoke an SSO session issued for an unexpected identity."""
    try:
        response = await client.logout()
    except Exception:
        logger.warning(
            "matrix_unexpected_sso_session_revoke_failed",
            expected_user_id=expected_user_id,
            actual_user_id=actual_user_id,
            exc_info=True,
        )
        return
    if isinstance(response, nio.ErrorResponse):
        logger.warning(
            "matrix_unexpected_sso_session_revoke_failed",
            expected_user_id=expected_user_id,
            actual_user_id=actual_user_id,
            error=str(response),
        )


async def login_flows(
    homeserver: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return login methods advertised by one Matrix homeserver."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, http_headers=http_headers)
    try:
        response = await client.login_info()
    finally:
        await client.close()
    if isinstance(response, nio.LoginInfoResponse):
        return tuple(response.flows)
    msg = f"Failed to query Matrix login methods: {response}"
    raise matrix_startup_error(msg, response=response)


async def restore_login(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Restore one authenticated Matrix session without creating a new device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
    )
    client.restore_login(user_id, device_id, access_token)

    response = await client.whoami()
    if isinstance(response, nio.WhoamiResponse):
        client.user_id = response.user_id
        if response.device_id:
            client.device_id = response.device_id
        logger.info("matrix_login_restored", user_id=response.user_id, device_id=client.device_id)
        return client

    await client.close()
    msg = f"Failed to restore Matrix login for {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


__all__ = [
    "PermanentMatrixStartupError",
    "create_authenticated_client",
    "login",
    "login_flows",
    "login_with_token",
    "matrix_client",
    "matrix_client_config",
    "matrix_startup_error",
    "olm_store_dir",
    "olm_store_exists",
    "restore_login",
]
