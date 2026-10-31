"""One collector freezes shared nio observations inside the caller transaction."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from ..client.base_client import _SyncItem
from ..event_provenance import TimelineEventProvenance
from ..events import BadEvent, InviteMemberEvent, RoomMemberEvent, UnknownBadEvent
from ..exceptions import LocalProtocolError
from ..responses import SlidingSyncStateStub
from ..rooms import MatrixRoom
from .codec import freeze_event
from .model import OwnMembership, RecordKind, SyncRecord, encode_records

if TYPE_CHECKING:
    from .client import DurableSync


class Processor:
    def __init__(
        self,
        session: DurableSync,
        history_rooms: list[str],
        explicit_memberships: set[str],
        provenance: TimelineEventProvenance = TimelineEventProvenance.LIVE,
        *,
        suppress_ids: dict[str, set[str]] | None = None,
        stop_on_oversized: bool = False,
    ):
        self.session = session
        self.history_rooms = history_rooms
        self.explicit_memberships = explicit_memberships
        self.provenance = provenance
        self.rooms: dict[str, MatrixRoom] = {}
        self.members: set[tuple[str, str]] = set()
        self.records: list[SyncRecord] = []
        self.suppress_ids = suppress_ids or {}
        self.stop_on_oversized = stop_on_oversized
        self.oversized = False
        self.oversized_membership: OwnMembership | None = None
        self.processed_records = 0

    def consume(self, items: Iterable[_SyncItem]) -> None:
        session = self.session
        pending_bytes: int | None = None

        def flush() -> None:
            nonlocal pending_bytes
            if self.records:
                pending_bytes = session._publish_records(
                    tuple(self.records), pending_bytes=pending_bytes
                )
                self.records.clear()

        for item in items:
            if item.route == "encrypted_rooms":
                session._store.matrix.save_encrypted_rooms(
                    cast(Iterable[str], item.event)
                )
                continue
            if item.route in ("presence", "ephemeral"):
                continue
            room = item.room
            room_id = room.room_id if room else None
            change = None
            historical = item.provenance is TimelineEventProvenance.HISTORY
            # Matrix never encrypts state, so plaintext decrypted from an
            # m.room.encrypted event is never membership or room state.
            decrypted = (
                item.source is not None
                and item.source.get("type") == "m.room.encrypted"
            )
            member = (
                isinstance(item.event, (RoomMemberEvent, InviteMemberEvent))
                and not decrypted
            )
            if room is not None and room_id is not None:
                if (
                    not decrypted
                    and isinstance(item.event, (BadEvent, UnknownBadEvent))
                    and (
                        item.route in (None, "invite")
                        or "state_key" in item.event.source
                        or item.event.source.get("type")
                        in (
                            "m.room.member",
                            "m.room.create",
                            "m.room.power_levels",
                            "m.room.encryption",
                        )
                    )
                ):
                    raise LocalProtocolError("malformed durable room state")
                if item.section == "invite":
                    session.client.rooms.pop(room_id, None)
                self.rooms[room_id] = room
                if item.event is None:
                    if item.section == "leave":
                        if session._metadata.get(room_id, {}).get("membership") in (
                            "leave",
                            "ban",
                        ):
                            # An empty leave section also confirms local leave.
                            session._change_membership(
                                room_id, session._metadata[room_id]["membership"]
                            )
                            continue
                    elif room_id in self.explicit_memberships:
                        continue
                    assert item.section is not None
                    change = session._change_membership(room_id, item.section)
                    if change is None:
                        continue
                if member and not historical:
                    assert isinstance(item.event, (RoomMemberEvent, InviteMemberEvent))
                    self.members.add((room_id, item.event.state_key))
                    if item.event.state_key == session.client.user_id:
                        self.explicit_memberships.add(room_id)
                        change = session._change_membership(
                            room_id, item.event.membership
                        )
                member_deleted = (
                    isinstance(item.event, SlidingSyncStateStub)
                    and item.event.type == "m.room.member"
                )
                if member_deleted:
                    assert isinstance(item.event, SlidingSyncStateStub)
                    self.members.add((room_id, item.event.state_key))
                    if item.event.state_key == session.client.user_id:
                        self.explicit_memberships.add(room_id)
                        change = session._change_membership(room_id, "leave")
                if change is not None and change.previous == "join":
                    session._metadata[room_id]["baseline"] = False
                    if room_id not in self.history_rooms:
                        self.history_rooms.append(room_id)
                if not historical and (member_deleted or change is not None or member):
                    session.client.invalidate_outbound_session(room_id)
                    session._outbound.member_cache.pop(room_id, None)
                if item.event is not None and getattr(item.event, "source", {}).get(
                    "event_id"
                ) in self.suppress_ids.get(room_id, set()):
                    continue
            record = freeze_event(item)
            if room_id is not None:
                record = replace(
                    record,
                    membership=change,
                    membership_epoch=session._metadata.get(room_id, {}).get(
                        "membership_epoch", 0
                    ),
                    provenance=(
                        (
                            TimelineEventProvenance.HISTORY
                            if room_id in self.history_rooms
                            else item.provenance or self.provenance
                        )
                        if record.kind is RecordKind.TIMELINE
                        else record.provenance
                    ),
                )
            barrier = change is not None or member
            if (
                self.stop_on_oversized
                and len(encode_records((record,)).encode())
                > session.config.max_batch_bytes
            ):
                # The iterator already mutated nio. Commit that prefix and a
                # fenced loss together instead of rolling back and reusing it.
                self.oversized = True
                self.oversized_membership = record.membership
                break
            if barrier:
                flush()
            self.records.append(record)
            self.processed_records += 1
            if barrier or len(self.records) >= session.config.max_batch_records:
                flush()
        flush()

    def save(self) -> None:
        # Rejoin can replace a room while the iterator is running.
        for room_id in self.rooms:
            if room_id in self.session.client.rooms:
                self.rooms[room_id] = self.session.client.rooms[room_id]
        self.session._save_rooms(self.rooms, self.members)
