"""Classic sync transactions around nio's shared synchronous processor."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..api import Api
from ..client.base_client import _SyncItem
from ..crypto import Olm
from ..events import RoomMemberEvent
from ..exceptions import LocalProtocolError
from ..responses import SlidingSyncResponse, SyncResponse
from ..rooms import MatrixInvitedRoom, MatrixRoom
from ..store import MatrixStore, SqliteStore
from .codec import restore_event
from .crypto import CryptoMaintenance
from .model import (
    OwnMembership,
    RecordKind,
    SyncBatch,
    SyncRecord,
    encode_json,
    encode_records,
)
from .observations import TransientSections, deliver_transients
from .outbound import OutboundCrypto
from .projection import encode_member, encode_room, restore_room
from .recovery import Recovery
from .sliding import SlidingSource, SlidingSyncConfig
from .store import DurableStore
from .transport import HttpError, Transport

if TYPE_CHECKING:
    from ..client.async_client import AsyncClient


@dataclass(frozen=True, slots=True)
class DurableSyncConfig:
    max_response_bytes: int = 16 * 1024 * 1024
    max_batch_records: int = 256
    max_batch_bytes: int = 2 * 1024 * 1024
    max_pending_bytes: int = 64 * 1024 * 1024
    sync_timeout_ms: int = 30_000
    sync_filter: dict[str, Any] | str | None = None
    recovery_page_size: int = 100
    max_recovery_pages: int = 1_000
    sliding: SlidingSyncConfig | None = None
    to_device_only: bool = False

    def __post_init__(self) -> None:
        if self.to_device_only and (
            self.sync_filter is not None or self.sliding is not None
        ):
            raise ValueError(
                "to-device-only sync cannot use a custom filter or Sliding Sync"
            )
        if (
            min(
                self.max_response_bytes,
                self.max_batch_records,
                self.max_batch_bytes,
                self.max_pending_bytes,
                self.recovery_page_size,
                self.max_recovery_pages,
            )
            <= 0
            or self.sync_timeout_ms < 0
        ):
            raise ValueError("durable sync bounds must be positive")


class DurableSync:
    """One durable Matrix input stream and one application batch consumer."""

    def __init__(
        self, client: AsyncClient, store: DurableStore, config: DurableSyncConfig
    ):
        self.client = client
        self._store = store
        self.config = config
        self.stream_id = store.stream_id
        self._changed = asyncio.Event()
        self._closed = False
        self._running: asyncio.Task[None] | None = None
        self._poll: asyncio.Task[bytes] | None = None
        self._quiescing = False
        self._crypto_lock = asyncio.Lock()
        self._local_lock = asyncio.Lock()
        self._source_ready = asyncio.Event()
        self._source_ready.set()
        self._transport = Transport(client, config.max_response_bytes)
        self._crypto = CryptoMaintenance(client, store)
        self._crypto.restore()
        self._outbound = OutboundCrypto(self)
        self._metadata: dict[str, dict[str, Any]] = {}
        self._restore_rooms()
        mode = (
            "to_device"
            if config.to_device_only
            else "sliding"
            if config.sliding is not None
            else "classic"
        )
        source = store.database.execute_sql(
            "SELECT body FROM NioDurableCrypto WHERE kind='sync_source' AND key='current'"
        ).fetchone()
        previous = (
            json.loads(source[0]) if source else ("classic" if store.cursor else mode)
        )
        if previous != mode:
            raise LocalProtocolError("durable store transport cannot be changed")
        store.database.execute_sql(
            "INSERT OR REPLACE INTO NioDurableCrypto(kind,key,body) VALUES('sync_source','current',?)",
            (encode_json(mode),),
        )
        self._sliding = SlidingSource(self, config.sliding) if config.sliding else None
        self._recovery = Recovery(self)

    def _restore_rooms(self) -> None:
        for room_id, encoded in self._store.load_room_metadata().items():
            metadata = json.loads(encoded)
            members = {
                user_id: json.loads(member)
                for user_id, member in self._store.database.execute_sql(
                    "SELECT user_id,member FROM NioDurableMember WHERE room_id=?",
                    (room_id,),
                )
            }
            room = restore_room(room_id, metadata, members)
            self._metadata[room_id] = metadata
            if metadata.get("membership") in ("leave", "ban"):
                continue
            if isinstance(room, MatrixInvitedRoom):
                self.client.invited_rooms[room_id] = room
            else:
                self.client.rooms[room_id] = room

    def _assert_active(self) -> None:
        self.client._assert_not_disposed()
        if self._closed:
            raise LocalProtocolError("durable sync session is closed")

    def _decode_response(
        self, body: bytes
    ) -> tuple[SyncResponse | SlidingSyncResponse, TransientSections]:
        if len(body) > self.config.max_response_bytes:
            raise LocalProtocolError("sync response exceeds the durable input bound")
        try:
            root = json.loads(body)
            if not isinstance(root, dict):
                raise ValueError("response is not an object")
            if self.config.to_device_only:
                # This source owns no room or account observations, even if a
                # server unexpectedly includes them despite its sync filter.
                for section in ("rooms", "presence", "account_data"):
                    root.pop(section, None)
            if self._sliding is not None:
                transients: TransientSections = []
                extensions = root.get("extensions", {})
                if isinstance(extensions, dict):
                    for kind in ("typing", "receipts"):
                        section = extensions.pop(kind, {})
                        rooms = (
                            section.get("rooms", {})
                            if isinstance(section, dict)
                            else None
                        )
                        if isinstance(rooms, dict):
                            transients.extend(
                                (room_id, {"events": [raw]})
                                for room_id, raw in rooms.items()
                            )
                        else:
                            transients.append((None, []))
                response = SlidingSyncResponse.from_dict(root)
                if not isinstance(response, SlidingSyncResponse) or not response.pos:
                    raise ValueError("invalid Sliding response")
                return response, transients
            # Transient observations never decide whether durable data is accepted.
            transients = [(None, root.pop("presence", {}))]
            rooms = root.get("rooms", {})
            if isinstance(rooms, dict):
                for section in ("join", "leave"):
                    infos = rooms.get(section, {})
                    if isinstance(infos, dict):
                        for room_id, info in infos.items():
                            if isinstance(info, dict):
                                transients.append((room_id, info.pop("ephemeral", {})))
            response = SyncResponse.from_dict(root)
            if not isinstance(response, SyncResponse) or not response.next_batch:
                raise ValueError("invalid sync response")
            return response, transients
        except (ValueError, TypeError, KeyError) as error:
            raise LocalProtocolError("malformed durable sync response") from error

    async def _capture_response(
        self,
        body: bytes,
        *,
        full_state: bool = False,
        newly_joined_room: str | None = None,
    ) -> tuple[SyncResponse | SlidingSyncResponse, TransientSections]:
        # A local join/leave must not overtake input being decoded for capture.
        # Only parsing runs in the worker; projection and SQLite stay on the loop.
        async with self._local_lock:
            self._assert_active()
            generation = self._sliding.generation if self._sliding is not None else 0
            response = await asyncio.to_thread(self._decode_response, body)
            self._assert_active()
            with self._store.transaction():
                self._store.capture(body)
                self._store.save_continuation({
                    "full_state": full_state,
                    "newly_joined_room": newly_joined_room,
                    "transport": "sliding" if self._sliding else "classic",
                })
            self._recovery.response = response[0]
            if self._sliding is not None:
                self._sliding.accepted_generation = generation
            return response

    async def _accept_response(
        self,
        body: bytes,
        *,
        full_state: bool = False,
        newly_joined_room: str | None = None,
    ) -> None:
        response, transients = await self._capture_response(
            body, full_state=full_state, newly_joined_room=newly_joined_room
        )
        await self._prepare_pending(response)
        await deliver_transients(self.client, transients)

    def _change_membership(self, room_id: str, membership: str) -> OwnMembership | None:
        # The single successful local operation already owns its tenure change.
        # Until an authoritative response boundary, reported state is history.
        intent = self._read_local_intent()
        if (
            intent is not None
            and "sequence" in intent
            and intent["room_id"] == room_id
            and not intent.get("observed")
        ):
            return None
        metadata = self._metadata.setdefault(room_id, {})
        previous = metadata.get("membership")
        epoch = metadata.get("membership_epoch", 0)
        if previous == membership:
            return None
        next_epoch = epoch + (previous == "join" and membership != "join")
        change = OwnMembership(previous, membership, epoch, next_epoch)
        metadata.update(membership=membership, membership_epoch=next_epoch)
        return change

    def _publish_records(
        self, records: tuple[SyncRecord, ...], *, pending_bytes: int | None = None
    ) -> int:
        """Publish bounded observations and return the transaction's queued bytes.

        Reuse the count only within an uninterrupted publication sequence.
        """
        if pending_bytes is None:
            pending_bytes = self._store.pending_bytes
        if len(records) > self.config.max_batch_records:
            limit = self.config.max_batch_records
            for index in range(0, len(records), limit):
                pending_bytes = self._publish_records(
                    records[index : index + limit], pending_bytes=pending_bytes
                )
            return pending_bytes
        encoded = encode_records(records)
        size = len(encoded.encode())
        if size > self.config.max_batch_bytes:
            if len(records) <= 1:
                raise LocalProtocolError(
                    "prepared event exceeds the durable batch bound"
                )
            middle = len(records) // 2
            pending_bytes = self._publish_records(
                records[:middle], pending_bytes=pending_bytes
            )
            return self._publish_records(records[middle:], pending_bytes=pending_bytes)
        if pending_bytes + size > self.config.max_pending_bytes:
            raise LocalProtocolError(
                "prepared output exceeds the durable pending bound"
            )
        self._store.publish(records, encoded_records=encoded)
        return pending_bytes + size

    def _timeline_room(
        self, room_id: str, room: MatrixRoom, event: object
    ) -> MatrixRoom:
        if (
            isinstance(event, RoomMemberEvent)
            and event.state_key == self.client.user_id
            and event.membership == "join"
            and self._metadata.get(room_id, {}).get("membership") not in (None, "join")
        ):
            intent = self._read_local_intent()
            if (
                intent is not None
                and "sequence" in intent
                and intent["room_id"] == room_id
                and not intent.get("observed")
            ):
                return room
            room = self._reset_joined_room(room_id)
            self._recovery.resets.add(room_id)
        return room

    def _reset_joined_room(self, room_id: str) -> MatrixRoom:
        previous = self.client.rooms.get(room_id)
        encrypted = room_id in self.client.encrypted_rooms or (
            previous is not None and previous.encrypted
        )
        room = MatrixRoom(room_id, self.client.user_id, encrypted)
        self.client.rooms[room_id] = room
        self.client.invited_rooms.pop(room_id, None)
        self._metadata.setdefault(room_id, {})["baseline"] = False
        self._outbound.member_cache.pop(room_id, None)
        self.client.invalidate_outbound_session(room_id)
        self._store.database.execute_sql(
            "DELETE FROM NioDurableMember WHERE room_id=?", (room_id,)
        )
        return room

    async def _prepare_pending(
        self, response: SyncResponse | SlidingSyncResponse | None = None
    ) -> None:
        self._assert_active()
        await self._recovery.prepare(response)

    def _save_rooms(
        self, rooms: dict[str, MatrixRoom], members: set[tuple[str, str]]
    ) -> None:
        database = self._store.database
        for room_id, room in rooms.items():
            if isinstance(room, MatrixInvitedRoom):
                database.execute_sql(
                    "DELETE FROM NioDurableMember WHERE room_id=?", (room_id,)
                )
                members.update((room_id, user_id) for user_id in room.users)
            prior = self._metadata.get(room_id, {})
            metadata = encode_room(
                room,
                membership=prior.get("membership"),
                membership_epoch=prior.get("membership_epoch", 0),
                members_complete=room.members_synced,
            )
            metadata["baseline"] = prior.get("baseline", False)
            metadata["nonjoined_cursor"] = prior.get("nonjoined_cursor")
            self._metadata[room_id] = metadata
            self._store.save_room_metadata(room_id, encode_json(metadata))
        for room_id, user_id in members:
            member = encode_member(rooms[room_id], user_id)
            if member is None:
                database.execute_sql(
                    "DELETE FROM NioDurableMember WHERE room_id=? AND user_id=?",
                    (room_id, user_id),
                )
            else:
                database.execute_sql(
                    "INSERT INTO NioDurableMember(room_id,user_id,member) VALUES(?,?,?) "
                    "ON CONFLICT(room_id,user_id) DO UPDATE SET member=excluded.member",
                    (room_id, user_id, encode_json(member)),
                )

    async def next_batch(self) -> SyncBatch | None:
        self._assert_active()
        return self._store.next_batch()

    @property
    def cursor(self) -> str | None:
        """Last atomically prepared Matrix position."""
        self._assert_active()
        return self._store.cursor

    @property
    def progress_generation(self) -> int:
        """Monotonic progress from committed publication and acknowledgement."""
        self._assert_active()
        return self._store.progress_generation

    async def dispatch(
        self, record: SyncRecord, *, event: object | None = None
    ) -> None:
        """Invoke one committed observation's callback without acknowledging it."""
        self._assert_active()
        if record.route is None:
            return
        room = None
        if record.room_id is not None:
            room = self.client.rooms.get(
                record.room_id
            ) or self.client.invited_rooms.get(record.room_id)
            if room is None:
                members = {
                    user_id: json.loads(member)
                    for user_id, member in self._store.database.execute_sql(
                        "SELECT user_id,member FROM NioDurableMember WHERE room_id=?",
                        (record.room_id,),
                    )
                }
                room = restore_room(
                    record.room_id, self._metadata[record.room_id], members
                )
        value = self.client._dispatch_sync_item(
            _SyncItem(
                record.route,
                event if event is not None else restore_event(record),
                room,
            )
        )
        if isawaitable(value):
            await value

    async def _maintain_crypto(self) -> None:
        await self._outbound.maintain()

    async def run(self) -> None:
        """Run one Classic source, draining committed batches between responses."""
        self._assert_active()
        if self._running is not None:
            raise LocalProtocolError("durable sync runner is already active")
        running = asyncio.current_task()
        assert running is not None
        self._running = running
        try:
            while not self._closed:
                self._assert_active()
                if self._store.has_batches():
                    self._changed.clear()
                    await self._changed.wait()
                    continue
                if self._store.has_input():
                    await self._recovery.advance()
                    if (
                        self._store.has_batches()
                        or (self._store.continuation or {}).get("phase") != "prepared"
                    ):
                        continue
                    await self._maintain_crypto()
                    with self._store.transaction():
                        self._store.finish_input()
                        self._recovery.response = None
                        self._store.publish((), completes_sync=True)
                    self._changed.set()
                    continue
                if (
                    self._outbound.maintenance_due
                    or self._crypto._pending()
                    or self.client.outgoing_to_device_messages
                ):
                    await self._maintain_crypto()
                    continue
                local = self._read_local_intent()
                if (
                    local is not None
                    and "sequence" not in local
                    and not self._local_lock.locked()
                ):
                    async with self._local_lock:
                        await self._apply_local_intent(local)
                    continue
                await self._source_ready.wait()
                if self._store.has_batches():
                    continue
                if self._quiescing or self._closed:
                    return
                newly_joined_room = (
                    self._recovery.newly_joined_room()
                    if self._sliding is None
                    else None
                )
                full_state = (
                    self._sliding is None
                    and newly_joined_room is None
                    and self._recovery.needs_full_state()
                )
                request_body = None
                if self._sliding is not None:
                    method, path, request_body = self._sliding.request()
                else:
                    method, path = Api.sync(
                        "",
                        self.cursor,
                        self.config.sync_timeout_ms,
                        (
                            None
                            if full_state or newly_joined_room
                            else (
                                {
                                    "room": {"rooms": []},
                                    "presence": {"types": []},
                                    "account_data": {"types": []},
                                }
                                if self.config.to_device_only
                                else self.config.sync_filter
                            )
                        ),
                        full_state=full_state or None,
                    )
                self._poll = asyncio.create_task(
                    self._transport.request(
                        method,
                        path,
                        request_body,
                        request_timeout=self.config.sync_timeout_ms / 1000 + 30,
                    )
                )
                try:
                    body = await self._poll
                    if self._poll is None:
                        # Local work took ownership after this poll completed.
                        # Keep the cursor so its unaccepted interval is replayed.
                        continue
                except asyncio.CancelledError:
                    # An owned poll can be interrupted for local work. External
                    # cancellation of the runner still propagates normally.
                    if not running.cancelling():
                        continue
                    raise
                except HttpError as error:
                    if (
                        self._sliding is not None
                        and error.status == 400
                        and error.errcode == "M_UNKNOWN_POS"
                        and self._sliding.unknown_positions < 4
                    ):
                        self._sliding.pos = None
                        self._sliding.unknown_positions += 1
                        await asyncio.sleep(
                            min(0.5 * 2 ** (self._sliding.unknown_positions - 1), 30)
                        )
                        continue
                    raise
                finally:
                    self._poll = None
                await self._accept_response(
                    body, full_state=full_state, newly_joined_room=newly_joined_room
                )
        finally:
            self._running = None
            self._changed.set()

    async def update_sliding_subscriptions(
        self, room_subscriptions: dict[str, Any]
    ) -> None:
        """Replace future subscriptions without discarding committed work."""
        self._assert_active()
        if self._sliding is None:
            raise LocalProtocolError("subscription updates require Sliding transport")
        if self._sliding.config.room_subscriptions == room_subscriptions:
            return
        self._sliding.config = replace(
            self._sliding.config, room_subscriptions=deepcopy(room_subscriptions)
        )
        # A fresh connection also removes sticky subscriptions absent from the new set.
        self._sliding.pos = None
        self._sliding.generation += 1
        if self._poll is not None:
            poll, self._poll = self._poll, None
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
        self._changed.set()

    def _read_local_intent(self) -> dict[str, Any] | None:
        return self._store.read_local_intent()

    def _local_position_matches(self, intent: dict[str, Any]) -> bool:
        metadata = self._metadata.get(intent["room_id"], {})
        return (
            "join" if metadata.get("membership") == "join" else "leave",
            metadata.get("membership_epoch", 0),
        ) == (intent["previous_membership"], intent["previous_epoch"])

    async def wait_for_membership_idle(self) -> None:
        """Wait for local acknowledgement and committed authoritative observation."""
        self._assert_active()
        while intent := self._read_local_intent():
            if "sequence" in intent:
                if intent["sequence"] <= self._store.acked_sequence and intent.get(
                    "observed"
                ):
                    with self._store.transaction():
                        self._store.delete_local_intent()
                    return
            self._changed.clear()
            await self._changed.wait()
            self._assert_active()

    def _room_ready(self, room_id: str) -> bool:
        # Without complete room state the client cannot know whether a room is
        # encrypted, so it sends plaintext only to rooms whose state is complete.
        room = self.client.rooms.get(room_id)
        return room is not None and (
            room.encrypted or self._metadata.get(room_id, {}).get("baseline", False)
        )

    async def wait_for_room_ready(self, room_id: str) -> bool:
        """Wait until the client can send messages to a joined room.

        A room this client joins, by ``change_membership()`` or as sync first
        reports it, has no established state until a sync delivers the room's
        complete state, and ``room_send()`` refuses plaintext there until then.
        The runner must keep running and consumer draining must continue while
        this waits: the runner does not poll while a batch is unacknowledged,
        so awaiting this while holding one, such as from a callback run by
        ``dispatch()``, does not return until the session closes unless the
        room can already accept sends.

        Return True once the client can send to the room, at once if it
        already can. Return False as soon as the client's own membership in
        the room, as this session has recorded it from sync or
        ``change_membership()``, is not join, including at the call: a room
        sync has not reported yet, such as one just made with
        ``room_create()``, returns False at once.
        Raise ``LocalProtocolError`` if the session closes while waiting.
        """
        self._assert_active()
        while self._metadata.get(room_id, {}).get("membership") == "join":
            if self._room_ready(room_id):
                return True
            self._changed.clear()
            await self._changed.wait()
            self._assert_active()
        return False

    async def change_membership(
        self,
        *,
        operation_id: UUID,
        room_id: str,
        previous_membership: str | None,
        previous_epoch: int,
        current_membership: str,
    ) -> bool:
        """Retain a local join/leave before HTTP; consumer draining must continue."""
        self._assert_active()
        if self.config.to_device_only:
            raise LocalProtocolError("to-device-only sync cannot own room membership")
        if (
            not room_id
            or current_membership not in ("join", "leave")
            or previous_membership not in ("join", "leave", None)
            or previous_epoch < 0
        ):
            raise ValueError("invalid local membership transition")
        intent = {
            "operation_id": str(operation_id),
            "room_id": room_id,
            "previous_membership": previous_membership or "leave",
            "previous_epoch": previous_epoch,
            "current_membership": current_membership,
        }
        await self.wait_for_membership_idle()
        async with self._local_lock:
            self._assert_active()
            if not self._local_position_matches(intent):
                return False
            if self._read_local_intent() is not None:
                raise LocalProtocolError("another local membership command is pending")
            with self._store.transaction():
                self._store.save_local_intent(intent, create=True)
            self._source_ready.clear()
            if self._poll is not None:
                self._poll.cancel()
                self._poll = None
            try:
                while self._store.has_input():
                    self._changed.clear()
                    await self._changed.wait()
                    self._assert_active()
                return await self._apply_local_intent(intent)
            finally:
                self._source_ready.set()
                self._changed.set()

    async def _apply_local_intent(self, intent: dict[str, Any]) -> bool:
        if not self._local_position_matches(intent):
            with self._store.transaction():
                self._store.delete_local_intent()
            self._changed.set()
            return False
        room_id = intent["room_id"]
        target = intent["current_membership"]
        if target == "join" and "join_baseline_cursor" not in intent:
            # A filtered sync can advance cursor without updating membership.
            # Only a nonjoined observation at this exact boundary proves a join.
            # Save the proof before HTTP; an uncertain retry must not refresh it.
            metadata = self._metadata.get(room_id, {})
            intent["join_baseline_cursor"] = (
                self.cursor
                if metadata.get("membership") in ("invite", "leave", "ban")
                and metadata.get("nonjoined_cursor") == self.cursor
                else None
            )
            with self._store.transaction():
                self._store.save_local_intent(intent)
        method, path = (
            Api.join("", room_id) if target == "join" else Api.room_leave("", room_id)
        )
        try:
            await self._transport.request(method, path, "{}")
        except HttpError:
            with self._store.transaction():
                self._store.delete_local_intent()
            self._changed.set()
            return False
        self._assert_active()
        try:
            with self._store.transaction():
                previous, epoch = (
                    intent["previous_membership"],
                    intent["previous_epoch"],
                )
                change = OwnMembership(
                    previous,
                    target,
                    epoch,
                    epoch + (previous == "join" and target == "leave"),
                    "local",
                )
                room = self.client.rooms.get(room_id) or MatrixRoom(
                    room_id, self.client.user_id
                )
                self._metadata.setdefault(room_id, {}).update(
                    membership=target,
                    membership_epoch=change.current_epoch,
                    baseline=False,
                )
                self._outbound.member_cache.pop(room_id, None)
                if target == "join":
                    room = self._reset_joined_room(room_id)
                    if self._sliding is not None:
                        self._sliding.forget_room(room_id)
                else:
                    self.client.rooms.pop(room_id, None)
                    if self.client.olm:
                        self.client.olm.outbound_group_sessions.pop(room_id, None)
                self.client.invited_rooms.pop(room_id, None)
                self._save_rooms({room_id: room}, set())
                batch = self._store.publish((
                    SyncRecord(
                        RecordKind.ROOM_LIFECYCLE,
                        room_id,
                        {"membership": target},
                        membership=change,
                        membership_epoch=change.current_epoch,
                    ),
                ))
                intent["sequence"] = batch.sequence
                self._store.save_local_intent(intent)
            self._changed.set()
            return True
        except BaseException:
            self.client._dispose()
            self._store.close()
            self._changed.set()
            raise

    async def quiesce(self) -> None:
        """Stop polling; finish captured input while the consumer keeps draining."""
        self._quiescing = True
        self._changed.set()
        if self._poll is not None:
            poll, self._poll = self._poll, None
            poll.cancel()
        if self._running is not None and self._running is not asyncio.current_task():
            await asyncio.shield(self._running)

    async def ack(self, batch: SyncBatch) -> None:
        self._assert_active()
        self._store.ack(batch)
        self._changed.set()

    async def wait_for_work(self) -> None:
        self._assert_active()
        while not self._store.has_batches():
            self._changed.clear()
            await self._changed.wait()
            self._assert_active()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        running = self._running
        try:
            if running is not None and running is not asyncio.current_task():
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass
        finally:
            self._store.close()
            self.client._dispose()
            self._changed.set()


def open_durable_sync(
    client: AsyncClient,
    *,
    consumer_id: UUID,
    store_path: Path,
    database_name: str | None = None,
    config: DurableSyncConfig | None = None,
    source_store_class: type[MatrixStore] = SqliteStore,
) -> DurableSync:
    """Attach storage to an authenticated client before any sync or crypto use."""
    if not client.logged_in or not client.device_id:
        raise LocalProtocolError(
            "durable sync requires authenticated account and device"
        )
    if not client.config.encryption_enabled:
        raise LocalProtocolError("durable sync requires the SQLite crypto store")
    if (
        client.store is not None
        or client.olm is not None
        or client._ordinary_sync_started
    ):
        raise LocalProtocolError(
            "durable sync requires a fresh client without a loaded store"
        )
    client._assert_not_disposed()
    store = DurableStore(
        store_path,
        user_id=client.user_id,
        device_id=client.device_id,
        consumer_id=consumer_id,
        database_name=database_name,
        pickle_key=client.config.pickle_key,
        source_store_class=source_store_class,
    )
    try:
        with store.transaction():
            client.store = store.matrix
            client.store_path = str(store_path)
            if client.config.encryption_enabled:
                client.olm = Olm(
                    client.user_id,
                    client.device_id,
                    store.matrix,
                    replace_rotated_device_keys=client.config.replace_rotated_device_keys,
                )
            client.encrypted_rooms = store.matrix.load_encrypted_rooms()
            client.next_batch = store.cursor or ""
            client.loaded_sync_token = client.next_batch
            session = DurableSync(client, store, config or DurableSyncConfig())
            client._durable_session = session
        return session
    except BaseException:
        store.close()
        client._dispose()
        raise
