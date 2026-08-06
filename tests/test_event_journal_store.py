"""Backend-neutral contract for the event journal, projection, and outbox.

Every test here runs on SQLite and on PostgreSQL. A rule that holds on only one
backend is a rule MindRoom does not actually have.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    AdmissionResult,
    ConversationCursor,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    SettlementOutcome,
    delivery_transaction_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
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
