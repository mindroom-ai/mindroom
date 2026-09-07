"""Relation facts come from the journal when it has them, the server when it does not."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import nio
import pytest

from mindroom.matrix.relation_lookup import RelationLookup

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"


@dataclass
class FakeRelations:
    """The journal's answer about events it did or did not admit."""

    admitted: dict[str, str | None] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
        """Return whether one event was admitted, and its thread."""
        del room_id
        self.calls.append(event_id)
        if event_id not in self.admitted:
            return False, None
        return True, self.admitted[event_id]


def source(event_id: str, *, thread_id: str | None = None) -> dict[str, Any]:
    """Return one raw Matrix message source."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": "hi"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": "@alice:example.org",
        "origin_server_ts": 1_000,
        "type": "m.room.message",
        "content": content,
    }


@dataclass
class FakeClient:
    """A homeserver that answers exactly what a test set up."""

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: nio.RoomGetEventError | None = None
    fetches: list[str] = field(default_factory=list)

    async def room_get_event(self, room_id: str, event_id: str) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one event, or the configured failure."""
        self.fetches.append(event_id)
        if self.error is not None:
            return self.error
        raw = self.events.get(event_id)
        if raw is None:
            return nio.RoomGetEventError.from_dict({"errcode": "M_NOT_FOUND", "error": "gone"})
        return nio.RoomGetEventResponse.from_dict({**raw, "room_id": room_id})


def lookup(relations: FakeRelations, client: FakeClient | None = None) -> RelationLookup:
    """Return a lookup over one fake journal and homeserver."""
    return RelationLookup(store=relations, runtime=SimpleNamespace(client=client))  # type: ignore[arg-type]


class TestThreadId:
    """An admitted event never costs a round trip."""

    async def test_an_admitted_thread_reply_answers_without_the_server(self) -> None:
        """An admitted thread reply answers without the server."""
        relations = FakeRelations(admitted={"$reply": "$root"})
        client = FakeClient()

        assert await lookup(relations, client).thread_id(ROOM, "$reply") == "$root"
        assert client.fetches == []

    async def test_an_admitted_room_event_answers_none_without_the_server(self) -> None:
        """Being in no thread is an answer, not an absence of one."""
        relations = FakeRelations(admitted={"$room-message": None})
        client = FakeClient()

        assert await lookup(relations, client).thread_id(ROOM, "$room-message") is None
        assert client.fetches == []

    async def test_an_unseen_event_is_fetched(self) -> None:
        """An unseen event is fetched."""
        relations = FakeRelations()
        client = FakeClient(events={"$old": source("$old", thread_id="$root")})

        assert await lookup(relations, client).thread_id(ROOM, "$old") == "$root"
        assert client.fetches == ["$old"]

    async def test_an_event_the_server_lost_has_no_thread(self) -> None:
        """An event the server lost has no thread."""
        relations = FakeRelations()
        client = FakeClient()

        assert await lookup(relations, client).thread_id(ROOM, "$gone") is None


class TestAdmittedThreadId:
    """The journal-only lookup, for callers that must not pay a round trip."""

    async def test_an_admitted_thread_reply_answers_from_the_journal(self) -> None:
        """An admitted thread reply answers from the journal."""
        relations = FakeRelations(admitted={"$reply": "$root"})
        client = FakeClient()

        assert await lookup(relations, client).admitted_thread_id(ROOM, "$reply") == "$root"
        assert client.fetches == []

    async def test_an_unseen_event_is_never_fetched(self) -> None:
        """The whole point: `thread_id` would fetch here, and this must not.

        The degraded replay guard runs this over a page of recent room events.
        One fetch per unproven event turns a single skipped message into
        hundreds of sequential requests inside a turn.
        """
        relations = FakeRelations()
        client = FakeClient(events={"$old": source("$old", thread_id="$root")})

        assert await lookup(relations, client).admitted_thread_id(ROOM, "$old") is None
        assert client.fetches == []

    async def test_the_contrast_is_real(self) -> None:
        """Same event, same fakes: `thread_id` fetches it and finds the thread."""
        relations = FakeRelations()
        client = FakeClient(events={"$old": source("$old", thread_id="$root")})

        assert await lookup(relations, client).thread_id(ROOM, "$old") == "$root"
        assert client.fetches == ["$old"]

    async def test_an_admitted_room_event_is_reported_as_thread_less(self) -> None:
        """An admitted event in no thread answers None, same as an unseen one.

        The caller only acts on positive proof, so collapsing these two costs
        it nothing -- but it is a real collapse, and `thread_id` keeps them
        apart because its caller pays a fetch to tell them apart.
        """
        relations = FakeRelations(admitted={"$room-message": None})
        client = FakeClient()

        assert await lookup(relations, client).admitted_thread_id(ROOM, "$room-message") is None
        assert client.fetches == []

    async def test_the_boolean_decides_and_not_the_thread_it_came_with(self) -> None:
        """A store reporting "not admitted" is believed even if it names a thread.

        The two halves of the answer are separate facts, and only the first one
        is proof. Without this the gate is invisible: every store in reach
        pairs "not admitted" with no thread, so returning the thread unguarded
        passes every other test here.
        """

        class ConfusedRelations(FakeRelations):
            async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
                del room_id
                self.calls.append(event_id)
                return False, "$root"

        assert await lookup(ConfusedRelations()).admitted_thread_id(ROOM, "$reply") is None

    async def test_an_unreadable_journal_proves_nothing_and_still_does_not_fetch(self) -> None:
        """A store that raises must not escalate into a homeserver sweep."""

        class BrokenRelations(FakeRelations):
            async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
                del room_id, event_id
                msg = "journal unavailable"
                raise RuntimeError(msg)

        client = FakeClient(events={"$reply": source("$reply", thread_id="$root")})

        assert await lookup(BrokenRelations(), client).admitted_thread_id(ROOM, "$reply") is None
        assert client.fetches == []


class TestEventInfo:
    """Relation metadata, and what a failed lookup means."""

    async def test_a_missing_event_is_absent_not_an_error(self) -> None:
        """A deleted event is a real answer."""
        assert await lookup(FakeRelations(), FakeClient()).event_info(ROOM, "$gone") is None

    async def test_a_refused_lookup_raises(self) -> None:
        """A refusal is not the same as an absence, and must not be read as one."""
        client = FakeClient(error=nio.RoomGetEventError.from_dict({"errcode": "M_FORBIDDEN", "error": "nope"}))

        with pytest.raises(RuntimeError, match="Failed to resolve related Matrix event"):
            await lookup(FakeRelations(), client).event_info(ROOM, "$secret")

    async def test_without_a_client_nothing_can_be_resolved(self) -> None:
        """Without a client nothing can be resolved."""
        assert await lookup(FakeRelations(), None).event_info(ROOM, "$any") is None


class TestTurnScope:
    """One turn pays for one round trip per event."""

    async def test_a_repeated_lookup_inside_one_turn_fetches_once(self) -> None:
        """A repeated lookup inside one turn fetches once."""
        client = FakeClient(events={"$old": source("$old", thread_id="$root")})
        relation_lookup = lookup(FakeRelations(), client)

        async with relation_lookup.turn_scope():
            first = await relation_lookup.event_info(ROOM, "$old")
            second = await relation_lookup.event_info(ROOM, "$old")

        assert first is not None
        assert second is not None
        assert client.fetches == ["$old"]

    async def test_a_missing_event_is_remembered_as_missing(self) -> None:
        """Otherwise the cheapest answer to memoize is the one that is never memoized."""
        client = FakeClient()
        relation_lookup = lookup(FakeRelations(), client)

        async with relation_lookup.turn_scope():
            assert await relation_lookup.event_info(ROOM, "$gone") is None
            assert await relation_lookup.event_info(ROOM, "$gone") is None

        assert client.fetches == ["$gone"]

    async def test_the_memo_does_not_outlive_its_turn(self) -> None:
        """The memo does not outlive its turn."""
        client = FakeClient(events={"$old": source("$old")})
        relation_lookup = lookup(FakeRelations(), client)

        async with relation_lookup.turn_scope():
            await relation_lookup.event_info(ROOM, "$old")
        async with relation_lookup.turn_scope():
            await relation_lookup.event_info(ROOM, "$old")

        assert client.fetches == ["$old", "$old"]

    async def test_a_nested_scope_joins_the_one_around_it(self) -> None:
        """A nested scope joins the one around it rather than starting a second memo."""
        client = FakeClient(events={"$old": source("$old")})
        relation_lookup = lookup(FakeRelations(), client)

        async with relation_lookup.turn_scope():
            await relation_lookup.event_info(ROOM, "$old")
            async with relation_lookup.turn_scope():
                await relation_lookup.event_info(ROOM, "$old")
            await relation_lookup.event_info(ROOM, "$old")

        assert client.fetches == ["$old"]

    async def test_outside_a_turn_nothing_is_remembered(self) -> None:
        """Outside a turn nothing is remembered."""
        client = FakeClient(events={"$old": source("$old")})
        relation_lookup = lookup(FakeRelations(), client)

        await relation_lookup.event_info(ROOM, "$old")
        await relation_lookup.event_info(ROOM, "$old")

        assert client.fetches == ["$old", "$old"]
