"""Hydration, point refetch, and the strict/non-strict read split."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.event_journal import EventClass, EventKind
from mindroom.matrix.conversation_hydration import (
    _HYDRATED_PROMPT_WINDOW_MESSAGES,
    _MESSAGES_PAGE_LIMIT,
    ConversationHydrator,
    _HydrationError,
    _projected_from_event,
    _reduce_current_revision,
)
from mindroom.matrix.conversation_reads import _ConversationReader, _StaleConversationError
from mindroom.matrix.journal_ingress import inbound_event, projected_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def raw(
    event_id: str,
    body: str,
    *,
    sender: str = ALICE,
    ts: int = 1_000,
    thread_id: str | None = None,
    replaces: str | None = None,
    redacted: bool = False,
) -> dict[str, Any]:
    """Return one raw Matrix message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if replaces is not None:
        content["m.new_content"] = {"msgtype": "m.text", "body": body}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    source: dict[str, Any] = {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": {} if redacted else content,
    }
    if redacted:
        source["unsigned"] = {
            "redacted_because": {"type": "m.room.redaction", "sender": sender, "content": {}},
        }
    return source


def parse(source: dict[str, Any]) -> nio.Event:
    """Return the parsed nio event for one raw source."""
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


@dataclass
class FakeClient:
    """A homeserver that answers exactly what a test set up."""

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    reported_depth: int | None = 3
    relation_calls: int = 0
    history_pages: int = 0
    history_end_token: str | None = None
    # A room whose history outlives any startup walk, which is what a
    # long-lived room actually looks like.
    endless_history: bool = False
    # What each endless page is made of. A streamed answer is one original
    # followed by a run of edits, so a page of a busy MindRoom room carries far
    # fewer logical messages than it carries events.
    endless_originals_per_page: int = 1
    endless_edits_per_page: int = 0
    # Explicit (chunk, end) pages, for shapes a real server produces that the
    # endless generator cannot express.
    pages: list[tuple[list[dict[str, Any]], str | None]] | None = None
    repeat_last: bool = False

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one stored event."""
        del room_id
        source = self.events.get(event_id)
        if source is None:
            return nio.RoomGetEventError("M_NOT_FOUND")
        # nio builds this response by assignment rather than construction.
        response = nio.RoomGetEventResponse()
        response.event = parse(source)
        return response

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Yield stored relations, enforcing depth the way nio does."""
        del room_id, recurse
        self.relation_calls += 1
        sources = self.relations.get(event_id, [])
        # Mirrors nio: an empty page has no depth to report and nothing that
        # could have been truncated, so it is never rejected.
        if (
            minimum_recursion_depth is not None
            and sources
            and (self.reported_depth is None or self.reported_depth < minimum_recursion_depth)
        ):
            raise nio.InsufficientRecursionDepthError(minimum_recursion_depth, self.reported_depth)
        for source in sources:
            yield parse(source)

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
    ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
        """Return one page of history, then successful exhaustion."""
        del room_id, start, direction, limit
        self.history_pages += 1
        if self.pages is not None:
            index = min(self.history_pages - 1, len(self.pages) - 1) if self.repeat_last else self.history_pages - 1
            sources, end = self.pages[index]
            return nio.RoomMessagesResponse(ROOM, [parse(source) for source in sources], "start", end)
        if self.endless_history:
            return nio.RoomMessagesResponse(
                ROOM,
                self._endless_page(self.history_pages),
                "start",
                f"token-{self.history_pages}",
            )
        if self.history_pages > 1:
            return nio.RoomMessagesResponse(ROOM, [], "start", self.history_end_token)
        return nio.RoomMessagesResponse(
            ROOM,
            [parse(source) for source in self.history],
            "start",
            self.history_end_token,
        )

    def _endless_page(self, page: int) -> list[nio.Event]:
        """Return one page of an inexhaustible room."""
        events: list[nio.Event] = []
        for index in range(self.endless_originals_per_page):
            original = f"$page{page}-{index}"
            events.append(parse(raw(original, f"message {page}-{index}", ts=1_000 + page)))
            events.extend(
                parse(
                    raw(
                        f"{original}-edit{edit}",
                        f"message {page}-{index} v{edit}",
                        ts=1_001 + page + edit,
                        replaces=original,
                    ),
                )
                for edit in range(self.endless_edits_per_page)
            )
        return events


def hydrator(store: PrincipalStore, client: FakeClient, **bounds: int) -> ConversationHydrator:
    """Return a hydrator wired to a fake homeserver."""
    return ConversationHydrator(store=store, client=client, **bounds)  # type: ignore[arg-type]


async def admit_all(store: PrincipalStore, sources: Iterable[dict[str, Any]]) -> None:
    """Admit raw events as live traffic."""
    for source in sources:
        event = parse(source)
        await store.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE),
        )


async def bodies(store: PrincipalStore, thread_id: str | None = None) -> list[str]:
    """Return the visible bodies of one conversation."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return [str(m.content["body"]) for m in page.messages]


class TestRevisionReduction:
    """Hydration must reach the same answer the live projection would."""

    async def test_the_original_wins_when_there_are_no_edits(self) -> None:
        """The original wins when there are no edits."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")))
        assert original is not None

        revision = _reduce_current_revision(original, ())

        assert revision.event_id == "$m"
        assert revision.content["body"] == "first"

    async def test_the_newest_edit_wins(self) -> None:
        """The newest edit wins."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")))
        assert original is not None
        relations = [
            _projected_from_event(ROOM, parse(raw("$e1", "second", ts=2_000, replaces="$m"))),
            _projected_from_event(ROOM, parse(raw("$e2", "third", ts=3_000, replaces="$m"))),
        ]

        revision = _reduce_current_revision(original, [r for r in relations if r is not None])

        assert revision.content["body"] == "third"

    async def test_an_edit_from_another_sender_is_ignored(self) -> None:
        """An edit from another sender is ignored."""
        original = _projected_from_event(ROOM, parse(raw("$m", "first")))
        assert original is not None
        forged = _projected_from_event(
            ROOM,
            parse(raw("$e", "forged", sender=BOB, ts=9_000, replaces="$m")),
        )
        assert forged is not None

        revision = _reduce_current_revision(original, [forged])

        assert revision.content["body"] == "first"

    async def test_a_redacted_event_projects_to_nothing(self) -> None:
        """The server already stripped it; storing an empty body would show one."""
        assert _projected_from_event(ROOM, parse(raw("$m", "gone", redacted=True))) is None


class TestThreadHydration:
    """A thread is built from its root plus its whole relation tree."""

    async def test_a_thread_is_hydrated_once(self, alice: PrincipalStore) -> None:
        """A thread is hydrated once."""
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={
                "$root": [
                    raw("$reply", "reply", ts=2_000, thread_id="$root"),
                    raw("$edit", "reply edited", ts=3_000, replaces="$reply"),
                ],
            },
        )
        hydrate = hydrator(alice, client)

        await hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root")
        await hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert client.relation_calls == 1
        # The root carries no thread relation of its own, so reading the thread
        # has to merge it back in; a thread that starts at its first reply is
        # missing the message the whole thread is about.
        assert await bodies(alice, "$root") == ["root", "reply edited"]

    async def test_concurrent_readers_share_one_hydration(self, alice: PrincipalStore) -> None:
        """Concurrent readers share one hydration."""
        client = FakeClient(events={"$root": raw("$root", "root")}, relations={"$root": []})
        hydrate = hydrator(alice, client)

        await asyncio.gather(
            *(hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root") for _ in range(5)),
        )

        assert client.relation_calls == 1

    async def test_a_server_that_ignores_recurse_fails_the_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """No fallback: such a server silently returns only direct children.

        Omitting the depth is the only portable signal that ``recurse`` was not
        honored, because the number itself means different things on different
        servers.
        """
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
            reported_depth=None,
        )

        with pytest.raises(_HydrationError, match="recursion depth"):
            await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id="$root")

    async def test_a_shallow_reported_depth_is_accepted(self, alice: PrincipalStore) -> None:
        """Verified against a live Tuwunel: a complete page can report 0.

        Tuwunel reports the depth of the deepest event it returned, so a
        relation tree that is genuinely one level deep reports one level. A
        floor above zero would reject ordinary conversations.
        """
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
            reported_depth=0,
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root", "reply"]

    async def test_an_empty_relation_page_is_not_a_failure(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A message with no relations reports no depth, and that is fine."""
        client = FakeClient(
            events={"$root": raw("$root", "root")},
            relations={"$root": []},
            reported_depth=None,
        )

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root"]

    async def test_a_failed_hydration_is_retried_not_cached(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A failed hydration is retried not cached."""
        client = FakeClient(events={}, relations={})

        with pytest.raises(_HydrationError):
            await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        client.events["$root"] = raw("$root", "root")
        client.relations["$root"] = []
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

        assert await bodies(alice, "$root") == ["root"]


class TestRoomHydration:
    """Room history is walked once, and exhaustion is not a failure."""

    async def test_history_populates_the_conversation(self, alice: PrincipalStore) -> None:
        """History populates the conversation."""
        client = FakeClient(history=[raw("$b", "second", ts=2_000), raw("$a", "first", ts=1_000)])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await bodies(alice) == ["first", "second"]

    async def test_an_empty_chunk_without_an_end_token_is_exhaustion(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An empty chunk without an end token is exhaustion.

        This shape used to be read as failure and left rooms unready.
        """
        client = FakeClient(history=[raw("$a", "first")], history_end_token=None)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await bodies(alice) == ["first"]

    async def test_hydration_stops_once_the_prompt_window_is_full(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Hydration promises the prompt window, not a mirror of the room.

        A long-lived room has more history than any startup walk should read.
        Stopping once the window is full is the contract being met, not a
        shortfall: a caller needing older history paginates Matrix directly.
        """
        client = FakeClient(endless_history=True, endless_originals_per_page=_MESSAGES_PAGE_LIMIT)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == _HYDRATED_PROMPT_WINDOW_MESSAGES // _MESSAGES_PAGE_LIMIT
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_the_window_counts_messages_a_prompt_can_read_not_events(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edits revise a message; they do not fill the window with new ones.

        MindRoom streams by editing, so the ratio of Matrix events to logical
        messages in its own rooms is an order of magnitude, not a rounding
        error. A walk that stopped after a fixed number of pages would call a
        handful of messages a full prompt window and hydrate almost nothing.
        """
        client = FakeClient(
            endless_history=True,
            endless_originals_per_page=1,
            endless_edits_per_page=_MESSAGES_PAGE_LIMIT - 1,
        )
        window = 5

        await hydrator(alice, client, prompt_window_messages=window).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == window
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=window * 2)
        assert len(page.messages) == window

    async def test_a_room_of_nothing_but_edits_still_stops(self, alice: PrincipalStore) -> None:
        """The window is what hydration aims for, not what it will spend."""
        client = FakeClient(
            endless_history=True,
            endless_originals_per_page=1,
            endless_edits_per_page=_MESSAGES_PAGE_LIMIT - 1,
        )

        await hydrator(
            alice,
            client,
            prompt_window_messages=1_000,
            max_fetched_events=_MESSAGES_PAGE_LIMIT * 3,
        ).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == 3
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_an_empty_page_with_a_continuation_is_not_exhaustion(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A filtered page can be empty and still have history behind it.

        Only the missing continuation token means the server has run out.
        Treating an empty chunk as exhaustion stops the walk one page early and
        then records the short result as a hydrated conversation.
        """
        client = FakeClient(pages=[([], "more"), ([raw("$older", "older")], None)])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == 2
        assert await bodies(alice) == ["older"]

    async def test_a_token_that_does_not_move_stops_the_walk(self, alice: PrincipalStore) -> None:
        """An empty page adds nothing to the count the ceiling measures."""
        client = FakeClient(pages=[([], "same"), ([], "same")], repeat_last=True)

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert client.history_pages == 2

    async def test_hydration_does_not_create_pending_work(self, alice: PrincipalStore) -> None:
        """Hydration does not create pending work."""
        client = FakeClient(history=[raw("$a", "first")])

        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

        assert await alice.pending() == ()


class TestPointRefetch:
    """Redacting the visible edit is repaired by asking the server."""

    @staticmethod
    async def _redact_current_edit(store: PrincipalStore) -> None:
        await admit_all(
            store,
            [raw("$m", "first"), raw("$e", "second", ts=2_000, replaces="$m")],
        )
        redaction = nio.Event.parse_event(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 3_000,
                "type": "m.room.redaction",
                "redacts": "$e",
                "content": {},
            },
        )
        assert isinstance(redaction, nio.Event)
        await store.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION),
        )

    async def test_the_prior_edit_is_restored_when_the_server_still_has_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The prior edit is restored when the server still has it."""
        await admit_all(
            alice,
            [
                raw("$m", "first"),
                raw("$e1", "second", ts=2_000, replaces="$m"),
                raw("$e2", "third", ts=3_000, replaces="$m"),
            ],
        )
        redaction = parse(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 4_000,
                "type": "m.room.redaction",
                "redacts": "$e2",
                "content": {},
            },
        )
        await alice.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION),
        )
        client = FakeClient(
            events={"$m": raw("$m", "first")},
            relations={
                "$m": [
                    raw("$e1", "second", ts=2_000, replaces="$m"),
                    raw("$e2", "third", ts=3_000, replaces="$m", redacted=True),
                ],
            },
        )

        assert await hydrator(alice, client).refresh(
            (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0],
        )
        assert await bodies(alice) == ["second"]

    async def test_the_original_is_restored_once_superseded_edits_are_purged(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A server may have already reclaimed the superseded edits.

        Both answers are correct, because both are what every other Matrix
        client in the room sees.
        """
        await self._redact_current_edit(alice)
        client = FakeClient(events={"$m": raw("$m", "first")}, relations={"$m": []})

        assert await hydrator(alice, client).refresh(
            (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0],
        )
        assert await bodies(alice) == ["first"]

    async def test_a_message_the_server_lost_is_removed(self, alice: PrincipalStore) -> None:
        """A message the server lost is removed."""
        await self._redact_current_edit(alice)
        client = FakeClient(events={"$m": raw("$m", "first", redacted=True)}, relations={})

        assert await hydrator(alice, client).refresh(
            (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0],
        )
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_an_unreachable_server_keeps_the_message_hidden(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unreachable server keeps the message hidden."""
        await self._redact_current_edit(alice)
        client = FakeClient(events={}, relations={})

        assert not await hydrator(alice, client).refresh(
            (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0],
        )
        assert await bodies(alice) == []
        assert len(await alice.pending_refreshes(room_id=ROOM, thread_id=None)) == 1


class TestReadModes:
    """The two callers, and what each is allowed to see."""

    @staticmethod
    async def _hidden_message(store: PrincipalStore) -> None:
        await admit_all(store, [raw("$m", "first"), raw("$e", "deleted", ts=2_000, replaces="$m")])
        redaction = parse(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 3_000,
                "type": "m.room.redaction",
                "redacts": "$e",
                "content": {},
            },
        )
        await store.admit(
            inbound_event(ROOM, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            projected_event(ROOM, redaction, EventKind.REDACTION),
        )

    async def test_a_non_strict_read_omits_rather_than_waits(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A non strict read omits rather than waits."""
        await self._hidden_message(alice)
        client = FakeClient(events={}, relations={})
        reader = _ConversationReader(store=alice, hydrator=hydrator(alice, client))

        page = await reader.read(room_id=ROOM, thread_id=None, limit=10)

        assert page.messages == ()
        assert client.relation_calls == 0

    async def test_a_strict_read_repairs_before_returning(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A strict read repairs before returning."""
        await self._hidden_message(alice)
        client = FakeClient(events={"$m": raw("$m", "first")}, relations={"$m": []})
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )
        reader = _ConversationReader(store=alice, hydrator=hydrator(alice, client))

        page = await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)

        assert [m.content["body"] for m in page.messages] == ["first"]

    async def test_a_strict_read_fails_rather_than_omitting(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Prompt assembly cannot tell an omission from an absence."""
        await self._hidden_message(alice)
        client = FakeClient(events={}, relations={})
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )
        reader = _ConversationReader(store=alice, hydrator=hydrator(alice, client))

        with pytest.raises(_StaleConversationError):
            await reader.read_strict(room_id=ROOM, thread_id=None, limit=10)
