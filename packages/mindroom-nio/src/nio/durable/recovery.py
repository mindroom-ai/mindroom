"""Bounded chronological pagination around the shared nio event iterators."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from ..api import Api, MessageDirection
from ..client.base_client import _SyncItem
from ..client.sliding_sync import iter_sliding_sync
from ..event_provenance import TimelineEventProvenance
from ..events import BadEvent, RoomMemberEvent, UnknownBadEvent
from ..exceptions import LocalProtocolError
from ..responses import (
    RoomInfo,
    RoomMessagesResponse,
    SlidingSyncResponse,
    SyncResponse,
    Timeline,
)
from .model import OwnMembership, RecordKind, SyncRecord, encode_json
from .processor import Processor
from .projection import restore_room
from .transport import ResponseTooLarge

if TYPE_CHECKING:
    from .client import DurableSync

PAGE_BYTES = 2 * 1024 * 1024
CONTROL_BYTES = 2 * 1024 * 1024


class Recovery:
    def __init__(self, session: DurableSync):
        self.session = session
        self.response: SyncResponse | SlidingSyncResponse | None = None
        self.resets: set[str] = set()
        self._complete_state_seen = False

    def needs_full_state(self, *, excluding: str | None = None) -> bool:
        session = self.session
        if session.config.to_device_only:
            return False
        return bool(session.cursor) and (
            (not session._metadata and not self._complete_state_seen)
            or any(
                room_id != excluding
                and value.get("membership") == "join"
                and not value.get("baseline")
                for room_id, value in session._metadata.items()
            )
        )

    def newly_joined_room(self) -> str | None:
        session = self.session
        intent = session._read_local_intent()
        if (
            not session.cursor
            or intent is None
            or "sequence" not in intent
            or intent.get("observed")
            or intent["current_membership"] != "join"
            or intent.get("join_baseline_cursor") != session.cursor
            or session._metadata.get(intent["room_id"], {}).get("nonjoined_cursor")
            != session.cursor
            or self.needs_full_state(excluding=intent["room_id"])
        ):
            return None
        # Incremental sync includes initial state for a room joined since the
        # cursor. An own join event alone cannot prove that boundary.
        return intent["room_id"]

    def _rooms(self) -> list[tuple[str, RoomInfo, str]]:
        assert self.response is not None
        if isinstance(self.response, SlidingSyncResponse):
            return [
                (
                    room_id,
                    RoomInfo(
                        Timeline(room.timeline, room.limited, room.prev_batch),
                        [],
                        [],
                        [],
                    ),
                    "leave" if room.membership in ("leave", "ban") else "join",
                )
                for room_id, room in self.response.rooms.items()
                if room.membership != "invite" and not room.stripped_state
            ]
        return [
            (room_id, info, section)
            for section, rooms in (
                ("join", self.response.rooms.join),
                ("leave", self.response.rooms.leave),
            )
            for room_id, info in rooms.items()
        ]

    def _candidates(self) -> list[tuple[str, RoomInfo, str]]:
        if isinstance(self.response, SlidingSyncResponse):
            source = self.session._sliding
            assert source is not None
            return [
                (room_id, info, section)
                for room_id, info, section in self._rooms()
                if (room := self.response.rooms[room_id]).initial or room.limited
                if source.membership(room_id, room)[1]
                and source.baselines.get(room_id, {}).get("token")
                and room.prev_batch
            ]
        return [
            (room_id, info, section)
            for room_id, info, section in self._rooms()
            if info.timeline.limited
        ]

    def _poison(self) -> None:
        self.session.client._dispose()
        self.session._store.close()
        self.session._changed.set()

    def _loss(
        self,
        room_id: str,
        state: dict[str, Any],
        reason: str,
        membership: OwnMembership | None = None,
    ) -> None:
        session = self.session
        session._metadata.setdefault(room_id, {})["baseline"] = False
        if room_id not in state["history_rooms"]:
            state["history_rooms"].append(room_id)
        session._publish_records((
            SyncRecord(
                RecordKind.LOSS,
                room_id,
                {"reason": reason, "pages": state.get("pages", 0)},
                membership=membership,
                membership_epoch=session._metadata[room_id].get("membership_epoch", 0),
            ),
        ))

    def _reconcile_membership_boundary(self, room_id: str, membership: str) -> bool:
        session = self.session
        intent = session._read_local_intent()
        if (
            intent is None
            or "sequence" not in intent
            or intent["room_id"] != room_id
            or intent.get("observed")
        ):
            return False
        intent["observed"] = True
        session._store.save_local_intent(intent)
        change = (
            None
            if membership == intent["current_membership"]
            else session._change_membership(room_id, membership)
        )
        session._outbound.member_cache.pop(room_id, None)
        if (
            change is not None
            and change.previous == "join"
            and membership != "join"
            and session.client.olm
        ):
            session.client.invalidate_outbound_session(room_id)
        if membership in ("leave", "ban", "invite"):
            session.client.rooms.pop(room_id, None)
            if membership != "invite":
                session.client.invited_rooms.pop(room_id, None)
        reset = membership == "join" and intent["current_membership"] != "join"
        if change is not None:
            session._metadata[room_id]["baseline"] = False
        if reset:
            session._reset_joined_room(room_id)
        if change is not None:
            session._publish_records((
                SyncRecord(
                    RecordKind.ROOM_LIFECYCLE,
                    room_id,
                    {"membership": membership},
                    membership=change,
                    membership_epoch=change.current_epoch,
                ),
            ))
        return reset

    async def prepare(
        self, response: SyncResponse | SlidingSyncResponse | None = None
    ) -> None:
        session = self.session
        state = session._store.continuation
        if state is None or state.get("phase") == "prepared":
            return
        if response is not None:
            self.response = response
        if self.response is None:
            pending = session._store.input
            assert pending is not None
            decoded, _ = await asyncio.to_thread(session._decode_response, pending[0])
            session._assert_active()
            self.response = decoded
        if state.get("phase") == "recover":
            return
        try:
            with session._store.transaction():
                if "phase" not in state:
                    state.update(
                        phase="tail",
                        old_cursor=session.cursor,
                        room_index=0,
                        history_rooms=[
                            room_id
                            for room_id, _, _ in self._rooms()
                            if not session._metadata.get(room_id, {}).get("baseline")
                        ],
                        explicit_rooms=[],
                        applied_ids={},
                    )
                    state["unknown_rooms"] = list(state["history_rooms"])
                    if isinstance(self.response, SlidingSyncResponse):
                        assert session._sliding is not None
                        session._sliding.plan(self.response, state)
                    if (session.cursor or session._sliding) and self._candidates():
                        state["phase"] = "recover"
                        self._next_room(state, 0)
                        processor = Processor(session, state["history_rooms"], set())
                        processor.consume(session.client._iter_to_device(self.response))
                        for room_id in state["unknown_rooms"]:
                            self._loss(room_id, state, "unknown authorization baseline")
                        processor.save()
                        session._crypto.capture()
                        session._store.save_continuation(state)
                        session._changed.set()
                        return
                    if session.cursor or session._sliding:
                        for room_id in state["unknown_rooms"]:
                            self._loss(room_id, state, "unknown authorization baseline")
                self._tail(state)
        except BaseException:
            self._poison()
            raise
        session._changed.set()

    def _next_room(self, state: dict[str, Any], index: int) -> None:
        candidates = self._candidates()
        start = state["old_cursor"]
        if self.session._sliding is not None and index < len(candidates):
            start = state["sliding_starts"][candidates[index][0]]
        state.update(
            room_index=index,
            **{
                "from": start,
                "pages": 0,
                "seen_tokens": [start],
                "limit": min(self.session.config.recovery_page_size, 100),
            },
        )
        if index >= len(candidates):
            state["phase"] = "tail"

    def _tail(self, state: dict[str, Any]) -> None:
        session = self.session
        assert self.response is not None
        response = self.response
        # A committed recovery prologue must never decrypt its Olm input twice.
        if "from" in state:
            response.to_device_events.clear()
        if isinstance(response, SlidingSyncResponse):
            self._sliding_tail(response, state)
            return
        explicit = set(state["explicit_rooms"])
        memberships = {}
        for room_id, info, section in self._rooms():
            own_membership = next(
                (
                    event.membership
                    for event in reversed((*info.state, *info.timeline.events))
                    if isinstance(event, RoomMemberEvent)
                    and event.state_key == session.client.user_id
                ),
                None,
            )
            memberships[room_id] = own_membership or section
            if own_membership is not None:
                explicit.add(room_id)
            applied = set(state["applied_ids"].get(room_id, []))
            # State is authoritative at the tail boundary, even when a room
            # reset discarded an earlier application of the same event.
            info.timeline.events = [
                event
                for event in info.timeline.events
                if event.source.get("event_id") not in applied
            ]
            for event in info.state:
                room = session.client.rooms.get(room_id)
                if room is not None:
                    session._timeline_room(room_id, room, event)
            if room_id not in session.client.rooms:
                session._store.database.execute_sql(
                    "DELETE FROM NioDurableMember WHERE room_id=?", (room_id,)
                )
        self.resets.clear()
        processor = Processor(
            session,
            state["history_rooms"],
            explicit,
            suppress_ids={
                room_id: set(ids) for room_id, ids in state["applied_ids"].items()
            },
        )
        processor.consume(session.client._iter_sync(response, include_left=True))
        complete_state = state.get("full_state") or (
            state["old_cursor"] is None and session.config.sync_filter is None
        )
        for room_id, _, section in self._rooms():
            metadata = session._metadata.setdefault(room_id, {})
            room_complete = complete_state or state.get("newly_joined_room") == room_id
            if section == "join" and room_complete and room_id not in self.resets:
                metadata["baseline"] = metadata.get("membership") == "join"
            if room_id in session.client.rooms:
                processor.rooms[room_id] = session.client.rooms[room_id]
            if (
                self._reconcile_membership_boundary(room_id, memberships[room_id])
                and memberships[room_id] == "join"
            ):
                processor.rooms[room_id] = session.client.rooms[room_id]
                processor.members = {
                    member for member in processor.members if member[0] != room_id
                }
            # Recovered departures belong to earlier positions; only the final
            # response section can prove nonjoined membership at next_batch.
            if section == "leave" and metadata.get("membership") in ("leave", "ban"):
                metadata["nonjoined_cursor"] = response.next_batch
        for room_id in response.rooms.invite:
            self._reconcile_membership_boundary(room_id, "invite")
            session._metadata[room_id]["nonjoined_cursor"] = response.next_batch
        if state.get("full_state"):
            # Another client can leave and forget a room before we sync its
            # departure. Full-state requests are unfiltered; use their captured
            # flag, not configuration that may have changed since capture.
            present = (
                response.rooms.join.keys()
                | response.rooms.leave.keys()
                | response.rooms.invite.keys()
            )
            for room_id, metadata in session._metadata.items():
                if metadata.get("membership") != "join" or room_id in present:
                    continue
                self._loss(room_id, state, "joined room absent from complete sync")
                processor.consume((
                    _SyncItem(room=session.client.rooms[room_id], section="leave"),
                ))
                self._reconcile_membership_boundary(room_id, "leave")
                metadata["nonjoined_cursor"] = response.next_batch
        processor.save()
        # Pre-boundary joined history must not restore a locally revoked room.
        for room_id in processor.rooms:
            if session._metadata[room_id].get("membership") in ("leave", "ban"):
                session.client.rooms.pop(room_id, None)
                session.client.invited_rooms.pop(room_id, None)
        session._crypto.capture()
        session._store.set_cursor(response.next_batch)
        session._store.save_continuation({"phase": "prepared"})
        session.client.next_batch = response.next_batch
        if complete_state:
            # Empty inventories still need discovery once after each restart.
            self._complete_state_seen = True

    def _sliding_tail(
        self, response: SlidingSyncResponse, state: dict[str, Any]
    ) -> None:
        session = self.session
        assert session._sliding is not None
        reset_rooms: set[str] = set()
        for room_id, sliding_room in response.rooms.items():
            if sliding_room.initial:
                session._store.database.execute_sql(
                    "DELETE FROM NioDurableMember WHERE room_id=?", (room_id,)
                )
                session._outbound.member_cache.pop(room_id, None)
        explicit = set(state["explicit_rooms"]) | {
            room_id
            for room_id, room in response.rooms.items()
            if room.membership != "invite" and not room.stripped_state
        }
        processor = Processor(session, state["history_rooms"], explicit)
        processor.consume(
            iter_sliding_sync(
                session.client,
                response,
                include_left=True,
                recovered_rooms=state["recovered_rooms"],
                history_rooms=state["history_rooms"],
                history_prefixes=state["sliding_history_prefixes"],
                suppress_ids={
                    room_id: set(ids) for room_id, ids in state["applied_ids"].items()
                },
            )
        )
        for room_id, sliding_room in response.rooms.items():
            metadata = session._metadata.setdefault(room_id, {})
            proof, _ = session._sliding.membership(room_id, sliding_room)
            metadata["baseline"] = (
                proof is not None
                and metadata.get("membership") == "join"
                and (sliding_room.initial or metadata.get("baseline", False))
            )
            if metadata.get("membership") == "join" and not metadata["baseline"]:
                reset_rooms.add(room_id)
            room = session.client.rooms.get(room_id)
            if room is not None:
                processor.rooms[room_id] = room
                if sliding_room.initial:
                    processor.members.update(
                        (room_id, user_id) for user_id in room.users
                    )
                elif sliding_room.heroes:
                    processor.members.update(
                        (room_id, hero.user_id)
                        for hero in sliding_room.heroes
                        if hero.user_id in room.users
                    )
            boundary = session._sliding.boundary_membership(sliding_room)
            if boundary in ("join", "leave", "invite", "ban") and (
                self._reconcile_membership_boundary(room_id, boundary)
            ):
                if boundary == "join":
                    reset_rooms.add(room_id)
                    processor.rooms[room_id] = session.client.rooms[room_id]
                    processor.members = {
                        member for member in processor.members if member[0] != room_id
                    }
        processor.save()
        for room_id in processor.rooms:
            if session._metadata[room_id].get("membership") in ("leave", "ban"):
                session.client.rooms.pop(room_id, None)
        session._sliding.commit(response, state)
        for room_id in reset_rooms | self.resets:
            session._sliding.forget_room(room_id)
        self.resets.clear()
        session._crypto.capture()
        session._store.set_cursor(response.pos)
        session._store.save_continuation({"phase": "prepared"})

    async def advance(self) -> None:
        await self.prepare()
        session = self.session
        state = session._store.continuation
        assert state is not None
        if session._store.has_batches() or state.get("phase") != "recover":
            return
        room_id, info, section = self._candidates()[state["room_index"]]
        target = info.timeline.prev_batch
        error = None
        page = None
        if room_id in state["unknown_rooms"]:
            # Already reported at capture; current state cannot fill this interval.
            self._commit_page(state, room_id, section, [], None, done=True)
            return
        if not isinstance(target, str) or not target or len(target) > 8192:
            error = "missing or oversized history boundary"
        elif state["from"] == target:
            self._commit_page(state, room_id, section, [], None, done=True)
            return
        elif state["pages"] >= min(session.config.max_recovery_pages, 1000):
            error = "history page limit exhausted"
        else:
            # An exclusive `to` can stop before the retained tail. Page forward
            # until its event IDs overlap, keeping the same page/control bounds.
            method, path = Api.room_messages(
                "",
                room_id,
                start=state["from"],
                direction=MessageDirection.front,
                limit=state["limit"],
                end=target if session._sliding is not None else None,
            )
            try:
                body = await session._transport.request(
                    method, path, max_bytes=PAGE_BYTES
                )
                raw = json.loads(body)
                if (
                    not isinstance(raw, dict)
                    or raw.get("start") != state["from"]
                    or not isinstance(raw.get("chunk"), list)
                ):
                    raise ValueError("invalid page envelope")
                if len(raw["chunk"]) > state["limit"]:
                    raise ResponseTooLarge
                if any(
                    not isinstance(event, dict)
                    or not isinstance(event.get("event_id"), str)
                    or not event["event_id"]
                    or len(event["event_id"]) > 8192
                    for event in raw["chunk"]
                ):
                    raise ValueError("invalid event identity")
                page = RoomMessagesResponse.from_dict(raw, room_id)
                if not isinstance(page, RoomMessagesResponse) or len(page.chunk) != len(
                    raw["chunk"]
                ):
                    raise ValueError("invalid history events")
                if any(
                    isinstance(event, (BadEvent, UnknownBadEvent))
                    for event in page.chunk
                ):
                    raise ValueError("malformed history event")
            except ResponseTooLarge:
                if state["limit"] > 1:
                    state["limit"] = max(1, state["limit"] // 2)
                    with session._store.transaction():
                        session._store.save_continuation(state)
                    return
                error = "oversized history event"
            except (LocalProtocolError, ValueError, TypeError, KeyError):
                error = "history unavailable or malformed"
        if error is not None:
            self._commit_page(state, room_id, section, [], None, done=True, loss=error)
            return
        assert isinstance(page, RoomMessagesResponse)
        tail_ids = {event.source.get("event_id") for event in info.timeline.events}
        previous_ids = state["applied_ids"].get(room_id, [])
        seen = set(previous_ids)
        events = []
        overlap = False
        for event in page.chunk:
            event_id = event.source["event_id"]
            if event_id in tail_ids:
                overlap = True
                if session._sliding is None:
                    break
                continue
            if event_id not in seen:
                events.append(event)
                seen.add(event_id)
        done = page.end == target or (
            (not page.chunk and page.end is None)
            if session._sliding is not None
            else overlap
        )
        if not done and (
            not isinstance(page.end, str)
            or not page.end
            or len(page.end) > 8192
            or page.end in state["seen_tokens"]
        ):
            error = "history boundary missing or token cycle"
        state["pages"] += 1
        state["applied_ids"][room_id] = list(seen)
        if page.end and not done and error is None:
            state["seen_tokens"].append(page.end)
        if len(encode_json(state).encode()) > CONTROL_BYTES:
            # Stop before applying this page; no unbounded identity ledger.
            state["applied_ids"][room_id] = previous_ids
            error = "history continuation bound exhausted"
            events = []
        self._commit_page(
            state,
            room_id,
            section,
            events,
            page.end,
            done=done or error is not None,
            loss=error,
        )

    def _commit_page(
        self,
        state: dict[str, Any],
        room_id: str,
        section: str,
        events: list[Any],
        end: str | None,
        *,
        done: bool,
        loss: str | None = None,
    ) -> None:
        session = self.session
        try:
            with session._store.transaction():
                processor = Processor(
                    session,
                    state["history_rooms"],
                    set(state["explicit_rooms"]),
                    TimelineEventProvenance.RECOVERED,
                    stop_on_oversized=True,
                )
                if events:
                    if room_id not in session.client.rooms:
                        members = {
                            user_id: json.loads(member)
                            for user_id, member in session._store.database.execute_sql(
                                "SELECT user_id,member FROM NioDurableMember WHERE room_id=?",
                                (room_id,),
                            )
                        }
                        session.client.rooms[room_id] = restore_room(
                            room_id, session._metadata[room_id], members
                        )
                    room = session.client.rooms[room_id]
                    encrypted: set[str] = set()
                    info = RoomInfo(Timeline(events, False, None), [], [], [])
                    processor.consume(
                        session.client._iter_room_timeline(
                            room_id, info, room, encrypted, section
                        )
                    )
                    if processor.oversized:
                        loss = "oversized recovered event"
                        done = True
                        # LOSS represents the already-applied oversized event;
                        # retain its identity and exclude only untouched suffix.
                        untouched = {
                            event.source["event_id"]
                            for event in events[processor.processed_records + 1 :]
                        }
                        state["applied_ids"][room_id] = [
                            event_id
                            for event_id in state["applied_ids"][room_id]
                            if event_id not in untouched
                        ]
                    room = session.client.rooms[room_id]
                    if room.encrypted and session.client.olm:
                        session.client.olm.update_tracked_users(room)
                    session.client.encrypted_rooms.update(encrypted)
                    session._store.matrix.save_encrypted_rooms(encrypted)
                if loss:
                    self._loss(room_id, state, loss, processor.oversized_membership)
                    if room_id in session.client.rooms:
                        processor.rooms[room_id] = session.client.rooms[room_id]
                state["explicit_rooms"] = list(processor.explicit_memberships)
                processor.save()
                session._crypto.capture()
                if done:
                    self._next_room(state, state["room_index"] + 1)
                else:
                    state["from"] = end
                session._store.save_continuation(state)
        except BaseException:
            self._poison()
            raise
        session._changed.set()
