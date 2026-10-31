"""Public crypto calls and maintenance share one retained HTTP request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ..api import Api
from ..client.base_client import _SyncItem
from ..event_builders import ToDeviceMessage
from ..events import MegolmEvent, RoomKeyRequest
from ..exceptions import LocalProtocolError, OlmUnverifiedDeviceError
from ..responses import (
    ErrorResponse,
    JoinedMembersResponse,
    KeysClaimResponse,
    KeysQueryResponse,
    KeysUploadResponse,
    Response,
    RoomKeyRequestResponse,
    ShareGroupSessionResponse,
    ToDeviceResponse,
)
from .codec import freeze_event
from .crypto import CryptoRequest

if TYPE_CHECKING:
    from ..crypto.to_device import EncryptedToDevice
    from .client import DurableSync


class OutboundCrypto:
    def __init__(self, session: DurableSync):
        self.session = session
        self.client = session.client
        self.store = session._store
        self.crypto = session._crypto
        self.member_cache: dict[str, list[str] | None] = {}
        self.maintenance_due = bool(
            self.crypto.olm.wedged_devices
            or self.crypto.olm.key_request_devices_no_session
        )

    def invalidate_users(self, users: Iterable[str]) -> None:
        changed = set(users)
        for room_id, group in list(self.crypto.olm.outbound_group_sessions.items()):
            recipients = self.member_cache.get(room_id) or ()
            if changed.intersection(recipients) or any(
                user_id in changed for user_id, _ in group.users_shared_with
            ):
                self.client.invalidate_outbound_session(room_id)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.session._assert_active()
        try:
            with self.store.transaction():
                yield
        except BaseException:
            self.client._dispose()
            self.store.close()
            self.session._changed.set()
            raise

    def change_key_share(self, event: RoomKeyRequest, cancel: bool = False) -> bool:
        with self.transaction():
            changed = self.crypto.change_key_share(event, cancel=cancel)
        if changed:
            self.maintenance_due = True
        self.session._changed.set()
        if changed and self.session._poll is not None:
            self.session._poll.cancel()
        return changed

    async def _send(self, request: CryptoRequest) -> Response:
        body = await self.session._transport.request(
            request.method, request.path, request.body
        )
        decoded = json.loads(body)
        with self.transaction():
            response, observations = self.crypto.apply(request, decoded)
            if isinstance(response, ErrorResponse):
                raise LocalProtocolError("invalid durable crypto response")
            if observations:
                self.session._publish_records(
                    tuple(
                        freeze_event(_SyncItem("to_device", event))
                        for event in observations
                    )
                )
        self.session._changed.set()
        return response

    async def _finish_pending(self) -> Response | None:
        if pending := self.crypto._pending():
            return await self._send(pending[0])
        return None

    async def maintain(self) -> None:
        async with self.session._crypto_lock:
            self.maintenance_due = False
            completed: set[str] = set()
            while True:
                with self.transaction():
                    request = self.crypto.next_request(completed=completed)
                if request is None:
                    return
                await self._send(request)
                completed.add(request.kind)

    async def to_device(
        self, message: ToDeviceMessage, tx_id: str | None = None
    ) -> ToDeviceResponse:
        async with self.session._crypto_lock:
            pending = self.crypto._pending()
            if pending is not None and (
                pending[0].request_id == tx_id
                or any(
                    key == pending[1] and previous is message
                    for key, previous in self.crypto._messages
                )
            ):
                response = await self._send(pending[0])
                assert isinstance(response, ToDeviceResponse)
                return response
            await self._finish_pending()
            with self.transaction():
                request = self.crypto.enqueue_message(message, request_id=tx_id)
            response = await self._send(request)
            assert isinstance(response, ToDeviceResponse)
            return response

    async def encrypted_to_device(
        self, operation: EncryptedToDevice, tx_id: str | None
    ) -> ToDeviceResponse:
        async with self.session._crypto_lock:
            pending = self.crypto._pending()
            if pending is not None:
                fingerprint = self.crypto.encrypted_fingerprint()
                same_id = pending[0].request_id == tx_id
                if same_id and fingerprint != operation.fingerprint:
                    raise LocalProtocolError(
                        "transaction ID belongs to different encrypted content"
                    )
                if same_id or (tx_id is None and fingerprint == operation.fingerprint):
                    response = await self._send(pending[0])
                    assert isinstance(response, ToDeviceResponse)
                    return response
            query = await self._keys_query({operation.message.recipient})
            device = operation.resolve(self.crypto.olm, query)
            if self.crypto.olm.session_store.get(device.curve25519) is None:
                await self._keys_claim({device.user_id: [device.id]})
            device = operation.resolve(self.crypto.olm, query)
            if self.crypto.olm.session_store.get(device.curve25519) is None:
                raise LocalProtocolError(
                    "encrypted to-device key claim supplied no valid session"
                )
            with self.transaction():
                device = operation.resolve(self.crypto.olm, query)
                message = operation.encrypt(self.crypto.olm, device)
                request = self.crypto.enqueue_message(
                    message,
                    request_id=tx_id,
                    encrypted_fingerprint=operation.fingerprint,
                )
            response = await self._send(request)
            assert isinstance(response, ToDeviceResponse)
            return response

    async def keys_upload(self) -> KeysUploadResponse:
        async with self.session._crypto_lock:
            response = await self._finish_pending()
            if isinstance(response, KeysUploadResponse):
                return response
            if not self.client.should_upload_keys:
                raise LocalProtocolError("No key upload needed.")
            with self.transaction():
                request = self.crypto.enqueue_upload()
            response = await self._send(request)
            assert isinstance(response, KeysUploadResponse)
            return response

    async def keys_query(self, user_set: set[str] | None = None) -> KeysQueryResponse:
        async with self.session._crypto_lock:
            return await self._keys_query(user_set)

    async def _keys_query(self, user_set: set[str] | None = None) -> KeysQueryResponse:
        response = await self._finish_pending()
        if user_set is None and isinstance(response, KeysQueryResponse):
            return response
        if not (self.crypto.olm.users_for_key_query if user_set is None else user_set):
            raise LocalProtocolError("No key query required.")
        with self.transaction():
            request = self.crypto.enqueue_query(user_set)
        response = await self._send(request)
        assert isinstance(response, KeysQueryResponse)
        return response

    async def keys_claim(self, users: Mapping[str, Iterable[str]]) -> KeysClaimResponse:
        async with self.session._crypto_lock:
            return await self._keys_claim(users)

    async def _keys_claim(
        self, users: Mapping[str, Iterable[str]]
    ) -> KeysClaimResponse:
        await self._finish_pending()
        with self.transaction():
            request = self.crypto.enqueue_claim(dict(users))
        response = await self._send(request)
        assert isinstance(response, KeysClaimResponse)
        return response

    async def joined_members(self, room_id: str) -> JoinedMembersResponse:
        async with self.session._crypto_lock:
            return await self._joined_members(room_id)

    async def _joined_members(self, room_id: str) -> JoinedMembersResponse:
        self.session._assert_active()
        method, path = Api.joined_members("", room_id)
        previous = self.member_cache.get(room_id)
        self.member_cache[room_id] = None
        try:
            body = await self.session._transport.request(method, path)
            response = JoinedMembersResponse.from_dict(json.loads(body), room_id)
            if not isinstance(response, JoinedMembersResponse):
                raise LocalProtocolError("invalid joined members response")
            if room_id not in self.member_cache:
                raise LocalProtocolError("room membership changed during lookup")
            users = [member.user_id for member in response.members]
            with self.transaction():
                if previous is None or set(previous) != set(users):
                    self.client.invalidate_outbound_session(room_id)
                if room_id in self.client.encrypted_rooms or (
                    (room := self.client.rooms.get(room_id)) is not None
                    and room.encrypted
                ):
                    self.crypto.olm.users_for_key_query.update(
                        set(users) - self.crypto.olm.tracked_users
                    )
                    self.crypto.capture()
            self.member_cache[room_id] = users
            return response
        finally:
            if self.member_cache.get(room_id) is None:
                self.member_cache.pop(room_id, None)

    async def ensure_members(self, room_id: str) -> None:
        async with self.session._crypto_lock:
            if room_id not in self.member_cache:
                await self._joined_members(room_id)

    async def request_room_key(
        self, event: MegolmEvent, tx_id: str | None = None
    ) -> RoomKeyRequestResponse:
        if event.session_id in self.client.outgoing_key_requests:
            raise LocalProtocolError(
                "A key sharing request is already sent out for this session id."
            )
        assert self.client.device_id is not None
        message = event.as_key_request(self.client.user_id, self.client.device_id)
        await self.to_device(message, tx_id)
        return RoomKeyRequestResponse(
            message.request_id, message.session_id, message.room_id, message.algorithm
        )

    async def share_group_session(
        self, room_id: str, ignore_unverified_devices: bool = False
    ) -> ShareGroupSessionResponse:
        room = self.client.rooms.get(room_id)
        if room is None or not room.encrypted:
            raise LocalProtocolError("group sharing requires a joined encrypted room")
        if room_id in self.client.sharing_session:
            raise LocalProtocolError("a group share is already in flight for this room")
        self.client.sharing_session[room_id] = asyncio.Event()
        try:
            if not room.members_synced:
                await self.ensure_members(room_id)
            users = (
                list(room.users) if room.members_synced else self.member_cache[room_id]
            )
            assert users is not None
            if missing := self.crypto.olm.get_missing_sessions(users):
                await self.keys_claim(missing)
            async with self.session._crypto_lock:
                await self._finish_pending()
                if (
                    self.client.rooms.get(room_id) is not room
                    or (
                        not room.members_synced
                        and self.member_cache.get(room_id) is not users
                    )
                    or (room.members_synced and users != list(room.users))
                ):
                    raise LocalProtocolError("room membership changed before sharing")
                olm = self.crypto.olm
                shared_with: set[tuple[str, str]] = set()
                chunks = olm.share_group_session_parallel(
                    room_id, users, ignore_unverified_devices
                )
                group = None
                while True:
                    trust_error = None
                    with self.transaction():
                        try:
                            chunk = next(chunks, None)
                        except OlmUnverifiedDeviceError as error:
                            # nio checks recipients before encrypting any chunk.
                            # Keep the newly created local group session; no
                            # ratchet mutation or send needs rolling back.
                            trust_error = error
                        else:
                            group = group or olm.outbound_group_sessions[room_id]
                            if chunk is not None:
                                recipients, content = chunk
                                request = self.crypto.enqueue_to_device(
                                    "m.room.encrypted", content
                                )
                    if trust_error is not None:
                        raise trust_error
                    if chunk is None:
                        break
                    assert group is not None
                    await self._send(request)
                    if olm.outbound_group_sessions.get(room_id) is not group:
                        raise LocalProtocolError(
                            "outbound group session changed while sharing"
                        )
                    group.users_shared_with.update(recipients)
                    shared_with.update(recipients)
                assert group is not None
                if olm.outbound_group_sessions.get(room_id) is not group:
                    raise LocalProtocolError(
                        "outbound group session changed while sharing"
                    )
                group.shared = True
                return ShareGroupSessionResponse(room_id, shared_with)
        finally:
            self.client.sharing_session.pop(room_id).set()
