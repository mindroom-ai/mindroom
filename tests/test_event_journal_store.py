"""Backend-neutral contract for the event journal, projection, and outbox.

Every test here runs on SQLite and on PostgreSQL. A rule that holds on only one
backend is a rule MindRoom does not actually have.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    AdmissionResult,
    ConversationCursor,
    DeliveryStage,
    DepartureObservation,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    SettlementOutcome,
    delivery_transaction_id,
)
from mindroom.event_journal.schema import (
    POSTGRES_DIALECT,
    SQLITE_DIALECT,
    added_columns,
    render,
    schema_statements,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.event_journal import PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def text(body: str) -> dict[str, object]:
    """Return a plain text message body."""
    return {"msgtype": "m.text", "body": body}


def edit(target: str, body: str) -> dict[str, object]:
    """Return an edit of ``target`` installing ``body``."""
    return {
        "msgtype": "m.text",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.text", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target},
    }


def message(
    event_id: str,
    *,
    sender: str = ALICE,
    ts: int = 1_000,
    content: Mapping[str, object] | None = None,
    thread_id: str | None = None,
    redacts: str | None = None,
    kind: EventKind = EventKind.MESSAGE,
    event_class: EventClass = EventClass.ACTIONABLE,
) -> tuple[InboundEvent, ProjectedEvent]:
    """Return the admission and projection views of one event."""
    body = dict(content) if content is not None else text(event_id)
    inbound = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        kind=kind,
        event_class=event_class,
        sender=sender,
        origin_server_ts=ts,
        source={"event_id": event_id, "content": body},
    )
    projected = ProjectedEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        sender=sender,
        origin_server_ts=ts,
        content=body,
        replaces_event_id=None,
        redacts_event_id=redacts,
    )
    return inbound, projected


async def admit(store: PrincipalStore, *args: object, **kwargs: object) -> AdmissionResult:
    """Admit one event built by ``message``."""
    inbound, projected = message(*args, **kwargs)  # type: ignore[arg-type]
    return await store.admit(inbound, projected)


async def bodies(store: PrincipalStore, *, thread_id: str | None = None, limit: int = 50) -> list[str]:
    """Return the visible bodies of one conversation, oldest first."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=limit)
    return [str(m.content["body"]) for m in page.messages]


async def test_other_admitted_room_event_excludes_the_current_event(alice: PrincipalStore) -> None:
    """Current admission alone means fresh; any other room admission means known history."""
    await admit(alice, "$current")

    assert not await alice.has_other_admitted_room_event(room_id=ROOM, event_id="$current")

    await admit(alice, "$prior", event_class=EventClass.CONTEXT_ONLY)

    assert await alice.has_other_admitted_room_event(room_id=ROOM, event_id="$current")
    assert not await alice.has_other_admitted_room_event(room_id=OTHER_ROOM, event_id="$current")


class TestPrincipalIsolation:
    """One database, many bots, no way to reach across."""

    async def test_bound_views_cannot_see_each_other(self, journal_store: EventJournalStore) -> None:
        """Bound views cannot see each other."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")

        await admit(first, "$only-mine")

        assert await bodies(first) == ["$only-mine"]
        assert await bodies(second) == []
        assert await second.load_event("$only-mine") is None
        assert [event.event_id for event in await first.pending()] == ["$only-mine"]
        assert await second.pending() == ()

    async def test_settling_is_bound_to_its_principal(self, journal_store: EventJournalStore) -> None:
        """Settling is bound to its principal."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")
        await admit(first, "$shared-id")
        await admit(second, "$shared-id")

        await second.settle("$shared-id", SettlementOutcome.SUCCEEDED)

        assert await first.is_pending("$shared-id")
        assert not await second.is_pending("$shared-id")


class TestAdmission:
    """The journal decides exactly once what MindRoom accepted."""

    async def test_admitting_twice_creates_one_pending_event(self, alice: PrincipalStore) -> None:
        """Admitting twice creates one pending event."""
        assert await admit(alice, "$one") is AdmissionResult.ADMITTED
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE

        assert [event.event_id for event in await alice.pending()] == ["$one"]

    async def test_pending_replays_in_receipt_order(self, alice: PrincipalStore) -> None:
        """Replay order is admission order, not the senders' clocks."""
        await admit(alice, "$late-clock", ts=9_000)
        await admit(alice, "$early-clock", ts=1_000)

        assert [event.event_id for event in await alice.pending()] == ["$late-clock", "$early-clock"]

    async def test_context_only_events_never_become_pending(self, alice: PrincipalStore) -> None:
        """Cold history populates the conversation without starting work."""
        await admit(alice, "$history", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.pending() == ()
        assert await bodies(alice) == ["$history"]

    async def test_settled_events_stay_out_of_replay(self, alice: PrincipalStore) -> None:
        """Settled events stay out of replay."""
        await admit(alice, "$one")
        await alice.settle("$one", SettlementOutcome.SUCCEEDED)

        assert await alice.pending() == ()
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE
        assert await alice.pending() == ()

    async def test_settlement_releases_the_replay_payload(self, alice: PrincipalStore) -> None:
        """The row outlives its payload: it is the proof, not the work item."""
        await admit(alice, "$one")
        await alice.settle("$one", SettlementOutcome.SUCCEEDED)

        settled = await alice.load_event("$one")
        assert settled is not None
        assert settled.source == {}

    async def test_replay_payload_survives_until_settlement(self, alice: PrincipalStore) -> None:
        """Replay payload survives until settlement."""
        await admit(alice, "$one")

        pending = await alice.pending()
        assert pending[0].source == {"event_id": "$one", "content": text("$one")}


class TestEditReduction:
    """One row per logical message, whatever order the events arrive in."""

    async def test_edit_replaces_the_visible_body(self, alice: PrincipalStore) -> None:
        """Edit replaces the visible body."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        assert await bodies(alice) == ["second"]

    async def test_older_edit_arriving_late_does_not_win(self, alice: PrincipalStore) -> None:
        """Older edit arriving late does not win."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$new", ts=3_000, content=edit("$original", "newest"))
        await admit(alice, "$old", ts=2_000, content=edit("$original", "stale"))

        assert await bodies(alice) == ["newest"]

    async def test_same_timestamp_edits_resolve_by_event_id(self, alice: PrincipalStore) -> None:
        """Timestamps tie; the total order has to come from somewhere."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_same_timestamp_edits_resolve_by_event_id_either_order(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Same timestamp edits resolve by event id either order."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_edit_before_original_applies_when_the_original_lands(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edit before original applies when the original lands."""
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))
        assert await bodies(alice) == []

        await admit(alice, "$original", content=text("first"))
        assert await bodies(alice) == ["second"]

    async def test_a_stranger_cannot_evict_the_authors_pending_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The unresolved-edit key includes the sender, and this is why.

        Without it, anyone could send an edit for a message that has not
        arrived yet and displace the real author's edit before it could apply.
        """
        await admit(alice, "$alice-edit", sender=ALICE, ts=2_000, content=edit("$original", "authored"))
        await admit(alice, "$bob-edit", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        await admit(alice, "$original", sender=ALICE, content=text("first"))

        assert await bodies(alice) == ["authored"]

    async def test_an_edit_from_another_sender_never_applies(self, alice: PrincipalStore) -> None:
        """An edit from another sender never applies."""
        await admit(alice, "$original", sender=ALICE, content=text("first"))
        await admit(alice, "$forged", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        assert await bodies(alice) == ["first"]

    async def test_only_the_latest_unresolved_edit_is_kept(self, alice: PrincipalStore) -> None:
        """Only the latest unresolved edit is kept."""
        await admit(alice, "$e1", ts=2_000, content=edit("$original", "one"))
        await admit(alice, "$e2", ts=3_000, content=edit("$original", "two"))
        await admit(alice, "$e3", ts=2_500, content=edit("$original", "middle"))

        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["two"]

    @pytest.mark.parametrize("edit_count", [1, 5, 25])
    async def test_edit_churn_leaves_one_row_and_no_history(
        self,
        alice: PrincipalStore,
        edit_count: int,
    ) -> None:
        """Streaming rewrites the same row; intermediate bodies are not stored."""
        await admit(alice, "$original", content=text("chunk 0"))
        for index in range(1, edit_count + 1):
            await admit(
                alice,
                f"$edit-{index:04d}",
                ts=1_000 + index,
                content=edit("$original", f"chunk {index}"),
            )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=100)
        assert len(page.messages) == 1
        assert page.messages[0].content["body"] == f"chunk {edit_count}"
        assert page.messages[0].logical_event_id == "$original"


class TestRedaction:
    """Deleted content stops being readable in the transaction that admits it."""

    async def test_redacting_the_original_removes_the_message(self, alice: PrincipalStore) -> None:
        """Redacting the original removes the message."""
        await admit(alice, "$original", content=text("secret"))
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_a_redacted_original_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """Backfill really does deliver a redaction before what it redacts."""
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)
        await admit(alice, "$original", content=text("secret"))

        assert await bodies(alice) == []

    async def test_a_redacted_edit_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """A redacted edit cannot be resurrected."""
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["first"]

    async def test_redacting_a_superseded_edit_changes_nothing(self, alice: PrincipalStore) -> None:
        """Redacting a superseded edit changes nothing."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit1", ts=2_000, content=edit("$original", "second"))
        await admit(alice, "$edit2", ts=3_000, content=edit("$original", "third"))

        await admit(alice, "$redaction", ts=4_000, redacts="$edit1", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert [m.content["body"] for m in page.messages] == ["third"]
        assert page.refresh_pending == ()

    async def test_redacting_the_visible_edit_hides_it_and_asks_for_a_refetch(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the visible edit hides it and asks for a refetch."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert [request.logical_event_id for request in page.refresh_pending] == ["$original"]

    async def test_no_read_of_any_kind_returns_the_redacted_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The gate is the cleared body, not the caller's willingness to wait."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        cursor_page = await alice.read_conversation(
            room_id=ROOM,
            thread_id=None,
            limit=50,
            before=ConversationCursor(created_ts=99_999, logical_event_id="$zzzzz"),
        )
        tiny_page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=1)

        for read in (page, cursor_page, tiny_page):
            assert all(m.content["body"] != "deleted" for m in read.messages)

    async def test_the_refresh_token_survives_a_restart(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A pending refetch is durable, so a crash cannot un-hide the content."""
        store = journal_store.principal("agent@alice")
        await admit(store, "$original", content=text("first"))
        await admit(store, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(store, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        reopened = journal_store.principal("agent@alice")
        page = await reopened.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert len(page.refresh_pending) == 1

    async def test_a_failed_refetch_keeps_the_message_hidden(self, alice: PrincipalStore) -> None:
        """A failed refetch keeps the message hidden."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        requests = await alice.pending_refreshes(room_id=ROOM, thread_id=None)
        assert len(requests) == 1

        still_pending = await alice.pending_refreshes(room_id=ROOM, thread_id=None)
        assert still_pending == requests

    async def test_a_thread_read_can_repair_its_own_root(self, alice: PrincipalStore) -> None:
        """A read that can see a message must be able to repair it.

        The root belongs to the room conversation, so a thread read merges it
        in. If the repair pass cannot see it by the same rule, a strict thread
        read raises forever: it reports the root as needing a refetch that
        nothing will ever be asked to perform.
        """
        await admit(alice, "$root", content=text("first"))
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        await admit(alice, "$edit", ts=3_000, content=edit("$root", "deleted"))
        await admit(alice, "$redaction", ts=4_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=50)
        assert [request.logical_event_id for request in page.refresh_pending] == ["$root"]

        requests = await alice.pending_refreshes(room_id=ROOM, thread_id="$root")

        assert [request.logical_event_id for request in requests] == ["$root"]

    async def test_a_successful_refetch_installs_the_server_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A successful refetch installs the server revision."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0]

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$original",
            revision_ts=1_000,
            content=text("first"),
        )

        assert installed
        assert await bodies(alice) == ["first"]
        assert await alice.pending_refreshes(room_id=ROOM, thread_id=None) == ()

    async def test_a_newer_edit_beats_an_in_flight_refetch(self, alice: PrincipalStore) -> None:
        """The refetch read the server before the newer edit existed."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        stale_request = (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0]

        await admit(alice, "$newer", ts=4_000, content=edit("$original", "newest"))

        installed = await alice.install_refetched_revision(
            stale_request,
            revision_event_id="$original",
            revision_ts=1_000,
            content=text("first"),
        )

        assert not installed
        assert await bodies(alice) == ["newest"]

    async def test_a_refetch_can_remove_a_message_the_server_lost(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A refetch can remove a message the server lost."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await alice.pending_refreshes(room_id=ROOM, thread_id=None))[0]

        assert await alice.drop_refetched_message(request)
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()


class TestBoundedReads:
    """Reads are paged; there is no call that returns a whole room."""

    async def test_a_read_requires_a_positive_limit(self, alice: PrincipalStore) -> None:
        """A read requires a positive limit."""
        with pytest.raises(ValueError, match="positive limit"):
            await alice.read_conversation(room_id=ROOM, thread_id=None, limit=0)

    async def test_pages_walk_backwards_without_gaps_or_repeats(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Pages walk backwards without gaps or repeats."""
        for index in range(25):
            await admit(alice, f"$m{index:03d}", ts=1_000 + index)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=7, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == [f"$m{index:03d}" for index in range(25)]

    async def test_a_page_is_chronological(self, alice: PrincipalStore) -> None:
        """A page is chronological."""
        for index in range(5):
            await admit(alice, f"$m{index}", ts=1_000 + index)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.created_ts for m in page.messages] == [1_000, 1_001, 1_002, 1_003, 1_004]

    async def test_threads_are_separate_conversations(self, alice: PrincipalStore) -> None:
        """Threads are separate conversations."""
        await admit(alice, "$room-message")
        await admit(alice, "$thread-message", thread_id="$root")

        assert await bodies(alice) == ["$room-message"]
        assert await bodies(alice, thread_id="$root") == ["$thread-message"]

    async def test_a_thread_read_includes_its_root(self, alice: PrincipalStore) -> None:
        """The root has no thread relation of its own, but the thread is about it."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_the_root_appears_once_even_when_it_is_also_a_reply(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The root appears once even when it is also a reply."""
        await admit(alice, "$root", ts=1_000, thread_id="$root")
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_a_thread_root_still_belongs_to_the_room_conversation(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread root still belongs to the room conversation."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice) == ["$root"]

    async def test_a_thread_page_respects_its_limit_with_the_root_merged(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread page respects its limit with the root merged."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=3)

        assert [m.logical_event_id for m in page.messages] == ["$reply2", "$reply3", "$reply4"]

    async def test_paging_a_thread_reaches_the_root_last(self, alice: PrincipalStore) -> None:
        """Paging a thread reaches the root last."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(
                room_id=ROOM,
                thread_id="$root",
                limit=2,
                before=cursor,
            )
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == ["$root", "$reply0", "$reply1", "$reply2", "$reply3", "$reply4"]

    async def test_ordering_agrees_with_python_for_mixed_case_ids(
        self,
        alice: PrincipalStore,
    ) -> None:
        """SQLite, PostgreSQL, and Python must agree on the cursor's order.

        PostgreSQL's default locale can sort ``'a'`` before ``'B'`` while
        SQLite and Python sort by byte. If the cursor column is not pinned to
        byte order, paging silently skips or repeats rows on one backend only.
        """
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [m.logical_event_id for m in page.messages] == sorted(identifiers)

    async def test_cursor_paging_matches_byte_order_across_a_timestamp_tie(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Cursor paging matches byte order across a timestamp tie."""
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=2, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == sorted(identifiers)


class TestAdmittedThreadId:
    """What the journal already knows about an event's place in a thread."""

    async def test_an_unseen_event_is_reported_as_unseen(self, alice: PrincipalStore) -> None:
        """An unseen event is reported as unseen, not as thread-less."""
        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$never") == (False, None)

    async def test_a_room_event_is_admitted_and_in_no_thread(self, alice: PrincipalStore) -> None:
        """These two answers are opposite situations and only one is worth a fetch."""
        await admit(alice, "$room-message")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$room-message") == (True, None)

    async def test_a_thread_reply_reports_its_root(self, alice: PrincipalStore) -> None:
        """A thread reply reports its root."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$reply") == (True, "$root")

    async def test_a_context_only_event_still_answers(self, alice: PrincipalStore) -> None:
        """Settlement clears the replay payload, not the relation the row records."""
        await admit(alice, "$context", thread_id="$root", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$context") == (True, "$root")

    async def test_another_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another room does not answer."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=OTHER_ROOM, event_id="$reply") == (False, None)


class TestLatestVisibleEvent:
    """The reply target a thread-blind client is pointed at."""

    async def test_an_empty_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """An empty thread has no latest event."""
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_the_newest_reply_wins(self, alice: PrincipalStore) -> None:
        """The newest reply wins."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$late"

    async def test_an_edited_message_answers_with_the_revision_on_screen(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An edit is the event actually in the room, so it is what a reply quotes."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child-edit"

    async def test_a_redacted_revision_answers_with_its_logical_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Quoting a deleted edit renders as nothing; the original is still there."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$child-edit")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=10)
        assert [r.logical_event_id for r in page.refresh_pending] == ["$child"]

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child"

    async def test_a_redacted_logical_event_falls_through_to_the_message_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the message itself removes the row, so the previous one answers."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$late")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$early"

    async def test_a_root_only_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """The root is stored in the room conversation, so a childless thread is empty.

        The caller falls back to the thread ID, which is the root's own event ID,
        so merging it here would only arrive at the same answer twice.
        """
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$root-edit", ts=2_000, content=edit("$root", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_another_thread_in_the_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another thread in the room does not answer."""
        await admit(alice, "$other", ts=9_000, thread_id="$other-root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_a_rejoin_stops_the_previous_membership_from_answering(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A rejoin can expose different history, so the old tail cannot be quoted."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$reply"

        await alice.advance_membership_epoch(ROOM)

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None


class TestSchemaUpgrade:
    """A database that predates a column has to gain it, not fail on it."""

    async def test_a_database_without_a_later_column_is_upgraded(self, tmp_path: Path) -> None:
        """`CREATE TABLE IF NOT EXISTS` leaves an existing table untouched.

        Which means a column added later never appears, and the failure lands
        at the first statement that names it rather than at startup. The
        journal runs in production, so every upgrade meets this.
        """
        database_path = tmp_path / "old.db"
        connection = sqlite3.connect(database_path)
        connection.execute(
            """
            CREATE TABLE visible_messages (
                principal_id TEXT NOT NULL, room_id TEXT NOT NULL, logical_event_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, sender TEXT NOT NULL, created_ts BIGINT NOT NULL,
                revision_event_id TEXT NOT NULL, revision_ts BIGINT NOT NULL, content_json TEXT,
                refresh_token BIGINT, membership_epoch BIGINT NOT NULL,
                PRIMARY KEY (principal_id, room_id, logical_event_id)
            )
            """,
        )
        connection.commit()
        connection.close()

        store = EventJournalStore.open_sqlite(database_path)
        try:
            principal = store.principal("agent@alice")
            inbound, projected = message("$m", sender=BOB, content=text("hello"))
            await principal.admit(inbound, projected)

            page = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=5)
            assert [m.content["body"] for m in page.messages] == ["hello"]
        finally:
            await store.close()

    async def test_a_card_table_predating_its_resolution_column_still_works(
        self,
        legacy_journal_store: EventJournalStore,
    ) -> None:
        """Approval cards shipped before decisions were recorded on them.

        Every statement naming ``resolution_json`` runs after store opening, so
        the upgrade has to have happened by then -- on both backends, since one
        guards the add itself and the other inspects the existing columns.
        """
        store = legacy_journal_store
        try:
            principal = store.principal("agent@alice")
            await principal.remember_approval_card(room_id=ROOM, card_event_id="$card", card={"body": "run it?"})

            stored = await principal.pending_approval_card(room_id=ROOM, card_event_id="$card")
            assert stored is not None
            assert stored.resolution is None

            await principal.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})
            resolved = await principal.pending_approval_card(room_id=ROOM, card_event_id="$card")
            assert resolved is not None
            assert resolved.resolution == {"status": "approved"}
            assert [c.resolution for c in await principal.pending_approval_cards(room_id=ROOM)] == [
                {"status": "approved"},
            ]

            await principal.forget_approval_card(card_event_id="$card")
            assert await principal.pending_approval_cards(room_id=ROOM) == ()
        finally:
            await store.close()

    async def test_a_membership_table_predating_its_departure_columns_still_works(
        self,
        legacy_journal_store: EventJournalStore,
    ) -> None:
        """Membership epochs shipped before departure bookkeeping sat beside them.

        A room already fenced by the old code has a row with no departure
        columns at all, and the very first departure observed after the upgrade
        reads them.
        """
        store = legacy_journal_store
        try:
            alice = store.principal("agent@alice")
            await alice.advance_membership_epoch(ROOM)

            local = await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
            assert local.observation is DepartureObservation.FENCED
            assert local.membership_epoch == 2

            reported = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)
            assert reported.observation is DepartureObservation.OWED_REPORT_CONSUMED
            assert await alice.membership_epoch(ROOM) == 2
        finally:
            await store.close()

    async def test_every_added_column_is_declared_in_the_table_too(self) -> None:
        """The two lists are edited by hand and drift silently otherwise."""
        statements = " ".join(schema_statements(SQLITE_DIALECT))
        for _table, column, _definition in added_columns():
            assert column in statements


class TestDeliveryIsScopedToTheMembershipThatAuthorizedIt:
    """A turn that outlived its membership must not answer into the next one.

    The fence deletes what the previous membership derived. Without this it
    would then write some of it straight back: a turn still running when the
    fence committed reaches enqueue afterwards, and the fence has been and
    gone.
    """

    async def test_a_turn_admitted_under_an_ended_membership_cannot_enqueue(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fence first, then enqueue: the enqueue is refused."""
        await admit(alice, "$turn")
        await alice.advance_membership_epoch(ROOM)

        transaction_id = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id is None
        assert await alice.load_delivery(turn_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_an_unattempted_row_enqueued_before_the_fence_is_deleted_by_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue first, then fence: the row goes with the membership."""
        await admit(alice, "$turn")
        assert (
            await alice.enqueue_delivery(
                turn_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )

        await alice.advance_membership_epoch(ROOM)

        assert await alice.load_delivery(turn_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_an_attempted_row_still_retries_after_a_fence_under_its_first_transaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery is a different object, and refusing it is worse.

        Its outcome is unknown and the homeserver may hold it already. Only
        presenting the identical transaction ID again collapses the retry onto
        the same event; refusing it would strand the row unacknowledged while
        leaving whatever it sent visible, and re-deriving a fresh transaction
        for it would guarantee the second answer rather than prevent it.
        """
        await admit(alice, "$turn")
        first = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="$turn", stage=DeliveryStage.FINAL)

        await alice.advance_membership_epoch(ROOM)

        retried = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )
        claimed = await alice.claim_delivery(turn_id="$turn", stage=DeliveryStage.FINAL)

        assert retried == first
        assert claimed is not None
        assert claimed.transaction_id == first
        assert claimed.payload["body"] == "answer"

    async def test_a_turn_the_journal_never_admitted_still_enqueues(self, alice: PrincipalStore) -> None:
        """A scheduled task is not a turn a membership authorized.

        There is no admission behind it and so no previous membership for its
        work to belong to. Refusing it would silence scheduled delivery in
        every room the bot has ever left and rejoined.
        """
        await alice.advance_membership_epoch(ROOM)

        transaction_id = await alice.enqueue_delivery(
            turn_id="scheduled-task-7",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("reminder"),
        )

        assert transaction_id is not None

    async def test_a_turn_under_the_current_membership_enqueues(self, alice: PrincipalStore) -> None:
        """The ordinary case still delivers."""
        await admit(alice, "$turn")

        transaction_id = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id == delivery_transaction_id("agent@alice", "$turn", "final")

    async def test_in_flight_transport_learns_the_membership_ended(self, alice: PrincipalStore) -> None:
        """Streaming edits never reach the outbox, so they ask this directly."""
        await admit(alice, "$turn")

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

        await alice.advance_membership_epoch(ROOM)

        assert not await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

    async def test_one_rooms_fence_does_not_silence_another_room(self, alice: PrincipalStore) -> None:
        """Leaving one room says nothing about a turn running in a different one."""
        await admit(alice, "$turn")

        await alice.advance_membership_epoch(OTHER_ROOM)

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)
        assert (
            await alice.enqueue_delivery(
                turn_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )


class TestDepartureBookkeeping:
    """One departure invalidates a room once, whichever observer sees it first."""

    async def test_a_consumed_report_leaves_the_new_projection_alone(self, alice: PrincipalStore) -> None:
        """Absorbing a report must not delete what the membership after it built."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        await admit(alice, "$fresh", ts=5_000)

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.logical_event_id for m in page.messages] == ["$fresh"]

    async def test_a_departure_with_no_report_owed_invalidates(self, alice: PrincipalStore) -> None:
        """A departure the bot never initiated drops what the old membership built."""
        await admit(alice, "$stale", ts=5_000)

        outcome = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert outcome.observation is DepartureObservation.FENCED
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert page.messages == ()

    async def test_owed_reports_are_scoped_to_one_principal(self, journal_store: EventJournalStore) -> None:
        """One bot's owed report must not absorb another bot's departure."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await bob.rooms_owing_departure_reports() == frozenset()
        assert (await bob.fence_departure(ROOM, source=DepartureSource.REPORTED)).fenced

    async def test_retiring_one_room_leaves_another_rooms_report_owed(self, alice: PrincipalStore) -> None:
        """Giving up on one room's report says nothing about any other room."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.fence_departure(OTHER_ROOM, source=DepartureSource.LOCAL)

        await alice.retire_owed_departure_reports(ROOM)

        assert await alice.rooms_owing_departure_reports() == frozenset({OTHER_ROOM})
        assert (await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)).fenced
        assert not (await alice.fence_departure(OTHER_ROOM, source=DepartureSource.REPORTED)).fenced


class TestByteOrderPinning:
    """Ordering that a cursor depends on must not vary with the server locale."""

    def test_the_cursor_comparison_is_pinned_to_byte_order(self) -> None:
        """turn_id and stage shipped unpinned and cannot be retyped in place.

        A PostgreSQL locale whose collation is not byte order would sort them
        differently from the cursor's own comparison, and recovery would skip
        rows or revisit them. CI cannot catch that: its PostgreSQL image uses
        musl locales, which all behave like C, so the two orderings agree there
        and diverge in a glibc deployment.
        """
        ordering = "ORDER BY created_at_ns, turn_id/*bytes*/, stage/*bytes*/"

        assert render(ordering, SQLITE_DIALECT) == "ORDER BY created_at_ns, turn_id, stage"
        assert render(ordering, POSTGRES_DIALECT) == ('ORDER BY created_at_ns, turn_id COLLATE "C", stage COLLATE "C"')

    def test_a_marker_inside_a_literal_is_refused(self) -> None:
        """Substitution is a plain rewrite and cannot tell a literal from an identifier.

        No statement embeds one today, and values are bound separately by both
        backends, so nothing user-controlled reaches the rewriter. The guard is
        there because the rewriter has no way to check that for itself.
        """
        with pytest.raises(ValueError, match="byte-order marker"):
            render("SELECT '/*bytes*/'", SQLITE_DIALECT)

    def test_a_statement_without_the_marker_is_untouched(self) -> None:
        """The rewrite must not perturb the statements that do not opt in."""
        assert render("SELECT 1", SQLITE_DIALECT) == "SELECT 1"
        assert render("SELECT 1", POSTGRES_DIALECT) == "SELECT 1"


class TestMembershipEpoch:
    """Leaving and rejoining invalidates what the previous membership saw."""

    async def test_hydration_is_recorded_per_membership(self, alice: PrincipalStore) -> None:
        """Hydration is recorded per membership."""
        epoch = await alice.membership_epoch(ROOM)
        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            expected_membership_epoch=epoch,
        )

        assert installed
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await bodies(alice) == ["$hydrated"]

    async def test_rejoining_invalidates_hydration(self, alice: PrincipalStore) -> None:
        """Rejoining invalidates hydration."""
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            expected_membership_epoch=epoch,
        )

        await alice.advance_membership_epoch(ROOM)

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_rejoining_drops_answers_the_previous_membership_never_sent(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unsent answer belongs to the conversation it was written for.

        Delivering it after a leave and rejoin would drop a reply to the old
        membership into the new one, where nothing asked for it.
        """
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        await alice.advance_membership_epoch(ROOM)

        assert await alice.unacknowledged_deliveries() == ()
        assert await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL) is None

    async def test_rejoining_keeps_an_answer_that_may_already_be_visible(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery has an outcome only the homeserver knows.

        Deleting it would free the turn to run again and post a second answer.
        The row is what makes the retry converge instead: it still holds the
        frozen payload and the transaction that goes with it.
        """
        transaction_id = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.advance_membership_epoch(ROOM)

        kept = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert kept is not None
        assert kept.transaction_id == transaction_id
        assert kept.payload["body"] == "answer"
        assert [delivery.turn_id for delivery in await alice.unacknowledged_deliveries()] == ["turn-1"]

    async def test_rejoining_keeps_an_answer_matrix_already_accepted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """That row is the record that the message is already visible."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$sent")

        await alice.advance_membership_epoch(ROOM)

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$sent"

    async def test_context_only_events_keep_no_payload(self, alice: PrincipalStore) -> None:
        """Otherwise the journal becomes the raw-event cache it replaces.

        A context-only event is projected at admission and never replayed, so
        it is admitted already settled — which means settlement, the step that
        clears a payload, never runs for it. Storing the source anyway retains
        every message the bot has ever seen, forever.
        """
        body = "x" * 500
        admission, projected = message("$history", content=text(body), event_class=EventClass.CONTEXT_ONLY)
        await alice.admit(admission, projected)

        stored = await alice.load_event("$history")

        assert stored is not None
        assert stored.source == {}
        assert await bodies(alice) == [body]

    async def test_actionable_events_keep_their_replay_payload(self, alice: PrincipalStore) -> None:
        """Compaction must not reach the events a crash has to replay."""
        admission, projected = message("$live", content=text("answer me"))
        await alice.admit(admission, projected)

        stored = await alice.load_event("$live")

        assert stored is not None
        assert stored.source != {}

    async def test_rejoining_removes_what_the_previous_membership_projected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the two memberships merge into one conversation.

        Dropping only the hydration marker leaves the old messages readable,
        so the next hydration adds the new membership's view on top of a
        history this membership may not be entitled to see at all.
        """
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$before")[1],),
            expected_membership_epoch=epoch,
        )
        assert await bodies(alice) == ["$before"]

        await alice.advance_membership_epoch(ROOM)

        assert await bodies(alice) == []

    async def test_rejoining_keeps_the_proof_that_an_event_was_answered(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The dedup record has to outlive any rejoin, or the turn runs twice."""
        admission, projected = message("$answered")
        await alice.admit(admission, projected)
        await alice.settle("$answered", SettlementOutcome.SUCCEEDED)

        await alice.advance_membership_epoch(ROOM)

        assert await alice.load_event("$answered") is not None
        assert await alice.admit(*message("$answered")) is AdmissionResult.DUPLICATE

    async def test_hydration_racing_a_rejoin_installs_nothing(self, alice: PrincipalStore) -> None:
        """A partly applied hydration would look complete to the next reader."""
        stale_epoch = await alice.membership_epoch(ROOM)
        await alice.advance_membership_epoch(ROOM)

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert await bodies(alice) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestOutbox:
    """Delivery survives a crash at every point around the network call."""

    async def test_the_transaction_id_is_derived_not_random(self) -> None:
        """The transaction id is derived not random."""
        first = delivery_transaction_id("agent@alice", "turn-1", "final")
        second = delivery_transaction_id("agent@alice", "turn-1", "final")
        other_stage = delivery_transaction_id("agent@alice", "turn-1", "initial")

        assert first == second
        assert first != other_stage

    async def test_enqueue_returns_the_same_transaction_across_restarts(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue returns the same transaction across restarts."""
        first = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        second = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert first == second

    async def test_an_unattempted_delivery_can_still_change(self, alice: PrincipalStore) -> None:
        """An unattempted delivery can still change."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("draft"),
        )
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("final"),
        )

        claimed = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert claimed is not None
        assert claimed.payload["body"] == "final"

    async def test_claiming_freezes_the_payload(self, alice: PrincipalStore) -> None:
        """Claiming freezes the payload.

        The case this closes: Matrix accepted the old text, and the regenerated
        text could never become visible under the same transaction ID.
        """
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "sent"

    async def test_reclaiming_returns_the_identical_delivery(self, alice: PrincipalStore) -> None:
        """Reclaiming returns the identical delivery."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        first = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        second = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        assert first == second

    async def test_unacknowledged_deliveries_are_replayable(self, alice: PrincipalStore) -> None:
        """Unacknowledged deliveries are replayable."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        assert [d.turn_id for d in await alice.unacknowledged_deliveries()] == ["turn-1"]

        await alice.acknowledge_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$sent",
        )

        assert await alice.unacknowledged_deliveries() == ()

    async def test_acknowledgement_keeps_the_first_event_id(self, alice: PrincipalStore) -> None:
        """Acknowledgement keeps the first event id."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$first")
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$second")

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$first"


class TestApprovalCards:
    """A card the bot sent stays answerable until its decision lands."""

    @staticmethod
    def card(event_id: str, *, sender: str = ALICE) -> dict[str, object]:
        """Return one approval-card event source."""
        return {
            "event_id": event_id,
            "sender": sender,
            "type": "io.mindroom.tool_approval",
            "content": {"approval_id": event_id.lstrip("$"), "status": "pending"},
        }

    async def test_a_remembered_card_reads_back_whole(self, alice: PrincipalStore) -> None:
        """A remembered card reads back whole, and unanswered."""
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.card == self.card("$card")
        assert stored.resolution is None

    async def test_a_recorded_decision_reads_back_with_the_card(self, alice: PrincipalStore) -> None:
        """A card keeps its decision until the room is known to show it.

        The decision is written before the Matrix edit is attempted, so this is
        what a crash between the two leaves behind, and it is what tells the
        next startup to redeliver rather than expire.
        """
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.resolution == {"status": "approved"}
        assert stored.card == self.card("$card")
        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.resolution for entry in scanned] == [{"status": "approved"}]

    async def test_a_second_decision_does_not_replace_the_first(self, alice: PrincipalStore) -> None:
        """The committed decision is the one that stands.

        A retry after a failed edit resends what was decided; letting a later
        write through would let a second click overwrite a decision whose tool
        already ran.
        """
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "denied"})

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.resolution == {"status": "approved"}

    async def test_a_decision_on_an_unknown_card_records_nothing(self, alice: PrincipalStore) -> None:
        """Resolving a card that was never stored must not create one."""
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None

    async def test_a_forgotten_card_is_gone(self, alice: PrincipalStore) -> None:
        """Resolving a card is what removes it, so presence means pending."""
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})
        await alice.forget_approval_card(card_event_id="$card")

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_a_card_is_not_readable_from_another_room(self, alice: PrincipalStore) -> None:
        """A card belongs to the room it was sent in."""
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))

        assert await alice.pending_approval_card(room_id=OTHER_ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=OTHER_ROOM) == ()

    async def test_remembering_twice_keeps_the_first_card(self, alice: PrincipalStore) -> None:
        """A repeated send acknowledgement must not rewrite the card body."""
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))
        await alice.remember_approval_card(
            room_id=ROOM,
            card_event_id="$card",
            card={**self.card("$card"), "sender": BOB},
        )

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.card["sender"] == ALICE

    async def test_a_rooms_cards_come_back_oldest_first(self, alice: PrincipalStore) -> None:
        """Startup expiry walks the room's cards in the order they were sent."""
        for index in range(3):
            await alice.remember_approval_card(
                room_id=ROOM,
                card_event_id=f"$card-{index}",
                card=self.card(f"$card-{index}"),
            )

        stored = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.card["event_id"] for entry in stored] == ["$card-0", "$card-1", "$card-2"]

    async def test_the_scan_honors_its_limit(self, alice: PrincipalStore) -> None:
        """A bounded scan is what tells the caller its own view was truncated."""
        for index in range(5):
            await alice.remember_approval_card(
                room_id=ROOM,
                card_event_id=f"$card-{index}",
                card=self.card(f"$card-{index}"),
            )

        assert len(await alice.pending_approval_cards(room_id=ROOM, limit=2)) == 2

    async def test_rejoining_makes_the_previous_memberships_cards_unrecoverable(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A card asked in a membership the bot has left is not this one's to answer.

        Expiring it would edit a message in a room the bot has since rejoined,
        answering a question nobody in the current membership asked.
        """
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))

        await alice.advance_membership_epoch(ROOM)

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_one_principals_cards_are_invisible_to_another(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Two bots in one database do not answer each other's approvals."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await alice.remember_approval_card(room_id=ROOM, card_event_id="$card", card=self.card("$card"))

        assert await bob.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await bob.pending_approval_cards(room_id=ROOM) == ()
        assert await alice.pending_approval_cards(room_id=ROOM) != ()


class TestConcurrency:
    """Concurrent conversations must not produce lock failures."""

    async def test_fifty_concurrent_conversations_admit_cleanly(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fifty concurrent conversations admit cleanly."""

        async def conversation(index: int) -> None:
            for step in range(10):
                inbound, projected = message(
                    f"$c{index:02d}-{step}",
                    ts=1_000 + step,
                    thread_id=f"$thread-{index:02d}",
                )
                await alice.admit(inbound, projected)

        await asyncio.gather(*(conversation(index) for index in range(50)))

        for index in range(50):
            assert len(await bodies(alice, thread_id=f"$thread-{index:02d}")) == 10

    async def test_concurrent_admissions_of_one_event_yield_one_pending(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Concurrent admissions of one event yield one pending."""
        inbound, projected = message("$contended")
        results = await asyncio.gather(*(alice.admit(inbound, projected) for _ in range(8)))

        assert results.count(AdmissionResult.ADMITTED) == 1
        assert [event.event_id for event in await alice.pending()] == ["$contended"]
