"""Independent event-set oracle for generated journal mutation sequences.

The model retains server facts, not the projection's SQL reduction algorithm.
It deliberately knows nothing about receipt counters, held-edit tables, or
refresh tokens. Only the runner translates those facts into journal inputs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom.event_journal import AdmissionResult, EventClass, EventKind, InboundEvent, ProjectedEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore, PrincipalStore, VisibleMessage


@dataclass(frozen=True)
class Action:
    """A printable, shrinkable command; references do not depend on earlier steps."""

    kind: Literal["admit", "settle", "conflicting_duplicate", "concurrent_duplicate", "reopen", "refresh"]
    principal: int = 0
    source: int = 0
    event: str = "message"


@dataclass(frozen=True)
class _Event:
    event_id: str
    room: str
    thread: str | None
    sender: str
    timestamp: int
    body: str
    target: str | None = None
    redacts: str | None = None
    kind: EventKind = EventKind.MESSAGE
    event_class: EventClass = EventClass.ACTIONABLE

    def views(self) -> tuple[InboundEvent, ProjectedEvent | None]:
        content: dict[str, object] = {"msgtype": "m.text", "body": self.body}
        if self.thread is not None:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": self.thread}
        if self.target is not None:
            # Replacement content claims a different thread. The logical
            # original must retain its placement regardless of this claim.
            content = {
                "msgtype": "m.text",
                "body": f"* {self.body}",
                "m.new_content": content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": self.target},
            }
        if self.kind is EventKind.REACTION:
            content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": self.body, "key": "+1"}}
        if self.kind is EventKind.REDACTION:
            content = {"redacts": self.redacts}
        inbound = InboundEvent(
            event_id=self.event_id,
            room_id=self.room,
            thread_id=self.thread,
            kind=self.kind,
            event_class=self.event_class,
            sender=self.sender,
            origin_server_ts=self.timestamp,
            source={"event_id": self.event_id, "sender": self.sender, "content": content},
        )
        projected = (
            None
            if self.kind is EventKind.REACTION
            else ProjectedEvent(
                event_id=self.event_id,
                room_id=self.room,
                thread_id=self.thread,
                sender=self.sender,
                origin_server_ts=self.timestamp,
                content=content,
                replaces_event_id=self.target,
                redacts_event_id=self.redacts,
            )
        )
        return inbound, projected


def _events(source: int) -> dict[str, _Event]:
    thread_index = source // 2
    room = f"!room{thread_index // 2}:example.org"
    root = f"$root{thread_index}"
    sender = f"@author{source % 2}:example.org"
    original = _Event(f"$s{source}", room, root, sender, 2 + source % 2, f"source-{source}")
    root_event = replace(
        original,
        event_id=root,
        thread=None,
        timestamp=1,
        body=f"root-{thread_index}",
        sender="@root-author:example.org",
        event_class=EventClass.CONTEXT_ONLY,
    )
    edit_a = replace(
        original,
        event_id=f"$s{source}-a",
        timestamp=4,
        body=f"edit-a-{source}",
        target=original.event_id,
        thread="$unrelated",
    )
    edit_z = replace(edit_a, event_id=f"$s{source}-z", body=f"edit-z-{source}")
    result = {
        "root": root_event,
        "message": original,
        "edit_a": edit_a,
        "edit_old": replace(edit_a, event_id=f"$s{source}-old", timestamp=3, body=f"old-edit-{source}"),
        "edit_z": edit_z,
        "forged_edit": replace(
            edit_a,
            event_id=f"$s{source}-forged",
            sender="@stranger:example.org",
            timestamp=99,
            body=f"forged-{source}",
        ),
        "edit_of_edit": replace(
            edit_a,
            event_id=f"$s{source}-nested",
            target=edit_a.event_id,
            timestamp=5,
            body=f"nested-{source}",
        ),
        "root_edit": replace(
            edit_a,
            event_id=f"$s{source}-root-edit",
            target=root,
            sender=root_event.sender,
            body=f"root-edit-{source}",
        ),
        "reaction": replace(original, event_id=f"$s{source}-reaction", kind=EventKind.REACTION, body=original.event_id),
    }
    for name in ("message", "edit_a", "edit_z", "root", "root_edit", "reaction"):
        target = result[name]
        result[f"redact_{name}"] = replace(
            original,
            event_id=f"$redact-{target.event_id[1:]}",
            kind=EventKind.REDACTION,
            timestamp=6,
            body="",
            redacts=target.event_id,
        )
    return result


_CATALOG = tuple(_events(index) for index in range(6))
_ROOMS = ("!room0:example.org", "!room1:example.org")
_SCOPES = (
    (_ROOMS[0], None),
    (_ROOMS[1], None),
    (_ROOMS[0], "$root0"),
    (_ROOMS[0], "$root1"),
    (_ROOMS[1], "$root2"),
    (_ROOMS[0], "$unrelated"),
    (_ROOMS[1], "$unrelated"),
)
_EVENT_IDS = tuple(event.event_id for catalog in _CATALOG for event in catalog.values())


@dataclass
class _Reference:
    seen: dict[str, _Event] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)

    def tombstones(self, room: str) -> frozenset[str]:
        return frozenset(event.redacts for event in self.seen.values() if event.room == room and event.redacts)

    def pending(self) -> list[str]:
        return [
            event.event_id
            for event in self.seen.values()
            if event.event_class is EventClass.ACTIONABLE
            and event.event_id not in self.settled
            and not (event.kind is EventKind.MESSAGE and event.event_id in self.tombstones(event.room))
        ]

    def originals(self, room: str, thread: str | None) -> list[_Event]:
        return sorted(
            (
                event
                for event in self.seen.values()
                if event.kind is EventKind.MESSAGE
                and event.target is None
                and event.room == room
                and thread in (event.thread, event.event_id)
                and event.event_id not in self.tombstones(room)
            ),
            key=lambda event: (event.timestamp, event.event_id),
        )

    def winner(self, original: _Event) -> _Event:
        deleted = self.tombstones(original.room)
        candidates = [original] + [
            event
            for event in self.seen.values()
            if event.target == original.event_id
            and event.sender == original.sender
            and event.room == original.room
            and event.event_id not in deleted
        ]
        return max(candidates, key=lambda event: (event.timestamp, event.event_id))

    def row(self, original: _Event) -> tuple[object, ...]:
        revision = self.winner(original)
        return (
            original.event_id,
            revision.event_id,
            original.sender,
            original.thread,
            original.timestamp,
            revision.timestamp,
            revision.body,
        )


def _row(message: VisibleMessage) -> tuple[object, ...]:
    return (
        message.logical_event_id,
        message.revision_event_id,
        message.sender,
        message.thread_id,
        message.created_ts,
        message.revision_ts,
        message.content["body"],
    )


class JournalFuzzRunner:
    """Exercise real storage and compare every action prefix with server facts."""

    def __init__(self, open_store: Callable[[], EventJournalStore]) -> None:
        self._open = open_store
        self.store = open_store()
        namespace = uuid4().hex
        self._principals = (f"fuzz-{namespace}-0", f"fuzz-{namespace}-1")
        self._models = (_Reference(), _Reference())

    async def close(self) -> None:
        """Release the real connections owned by this example."""
        await self.store.close()

    def _principal(self, index: int) -> PrincipalStore:
        return self.store.principal(self._principals[index])

    async def _admit(self, action: Action) -> None:
        event = _CATALOG[action.source][action.event]
        model = self._models[action.principal]
        principal = self._principal(action.principal)
        expected = AdmissionResult.DUPLICATE if event.event_id in model.seen else AdmissionResult.ADMITTED
        if action.kind == "concurrent_duplicate":
            results = await asyncio.gather(principal.admit(*event.views()), principal.admit(*event.views()))
            assert sorted(results) == sorted((expected, AdmissionResult.DUPLICATE))
        else:
            assert await principal.admit(*event.views()) is expected
        model.seen.setdefault(event.event_id, event)
        if action.kind == "conflicting_duplicate":
            conflicting = replace(event, body="conflicting replay", sender="@stranger:example.org", thread="$unrelated")
            assert await principal.admit(*conflicting.views()) is AdmissionResult.DUPLICATE

    async def check(self) -> tuple[object, ...]:
        """Check exact ordered history, complete pending work, and tombstones."""
        snapshot: list[object] = []
        for index, model in enumerate(self._models):
            principal = self._principal(index)
            pending = await principal.pending(limit=1000)
            assert [event.event_id for event in pending] == model.pending(), "pending receipt order or cardinality"
            for event in pending:
                expected_event = model.seen[event.event_id]
                assert event.source == expected_event.views()[0].source, "duplicate changed pending payload"
                assert (event.room_id, event.thread_id, event.sender, event.kind) == (
                    expected_event.room,
                    expected_event.thread,
                    expected_event.sender,
                    expected_event.kind,
                ), "pending event provenance"
            snapshot.append(tuple(pending))
            for room in _ROOMS:
                deleted = await principal.redacted_event_ids(room, _EVENT_IDS)
                assert deleted == model.tombstones(room), "redaction tombstone authority"
                snapshot.append(deleted)
            for room, thread in _SCOPES:
                page = await principal.read_conversation(room_id=room, thread_id=thread, limit=1000)
                originals = model.originals(room, thread)
                owed = {request.logical_event_id for request in page.refresh_pending}
                assert len(owed) == len(page.refresh_pending), "duplicate refresh debt"
                assert owed <= {event.event_id for event in originals}, "unknown refresh debt"
                for original in originals:
                    if original.event_id in owed:
                        assert any(
                            event.target == original.event_id
                            and event.sender == original.sender
                            and event.room == room
                            and event.event_id in model.tombstones(room)
                            for event in model.seen.values()
                        ), "hidden message without a redacted revision"
                expected = [model.row(event) for event in originals if event.event_id not in owed]
                actual = [_row(message) for message in page.messages]
                assert actual == expected, ("conversation history", index, room, thread, actual, expected)
                snapshot.append(page)
        return tuple(snapshot)

    async def _refresh(self) -> None:
        for index, model in enumerate(self._models):
            principal = self._principal(index)
            for room, thread in _SCOPES:
                page = await principal.read_conversation(room_id=room, thread_id=thread, limit=1000)
                for request in page.refresh_pending:
                    original = model.seen[request.logical_event_id]
                    winner = model.winner(original)
                    assert await principal.install_refetched_revision(
                        request,
                        revision_event_id=winner.event_id,
                        revision_ts=winner.timestamp,
                        revision_sender=winner.sender,
                        content={"msgtype": "m.text", "body": winner.body},
                    )
                repaired = await principal.read_conversation(room_id=room, thread_id=thread, limit=1000)
                assert not repaired.refresh_pending, "refresh debt did not settle"

    async def _check_pagination(self) -> None:
        for index, model in enumerate(self._models):
            for room, thread in _SCOPES:
                cursor = None
                rows: list[tuple[object, ...]] = []
                # At most three visible messages per thread, plus an empty
                # page if the last page is exactly full. Bound broken cursors.
                for _ in range(5):
                    page = await self._principal(index).read_conversation(
                        room_id=room,
                        thread_id=thread,
                        limit=2,
                        before=cursor,
                    )
                    assert not page.refresh_pending
                    rows = [_row(message) for message in page.messages] + rows
                    if page.next_cursor is None:
                        break
                    assert page.next_cursor != cursor, "pagination cursor did not advance"
                    cursor = page.next_cursor
                else:
                    msg = "pagination did not terminate"
                    raise AssertionError(msg)
                assert rows == [model.row(event) for event in model.originals(room, thread)], "history pagination"

    async def run(self, actions: list[Action]) -> None:
        """Replay the exact printed action list, validating every intermediate state."""
        for action in actions:
            if action.kind == "reopen":
                before = await self.check()
                await self.close()
                self.store = self._open()
                assert await self.check() == before, "reopen changed durable state"
            elif action.kind == "refresh":
                await self._refresh()
            elif action.kind == "settle":
                event = _CATALOG[action.source][action.event]
                await self._principal(action.principal).settle(event.event_id)
                model = self._models[action.principal]
                if event.event_id in model.seen:
                    model.settled.add(event.event_id)
            else:
                await self._admit(action)
            await self.check()
        await self._refresh()
        await self.check()
        await self._check_pagination()
