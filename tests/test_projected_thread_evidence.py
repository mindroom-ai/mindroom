"""What the projection may conclude about a thread it cannot fully see, and how a caller sees a fresh write."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.matrix.conversation_hydration import ConversationReadError
from mindroom.matrix.conversation_reads import ConversationReader, complete_thread_history, refetched_thread_history
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.projected_thread_membership import resolve_thread_root_event_id
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from mindroom.matrix.thread_membership import (
    ThreadResolutionState,
    resolve_related_event_thread_membership,
    thread_messages_thread_membership_access,
)
from mindroom.matrix.thread_mutation_impact import event_thread_membership_known, redaction_thread_membership_known
from tests.journal_helpers import record_room_history_recovery
from tests.journal_membership_helpers import admit_room_membership
from tests.test_conversation_hydration import ROOM, FakeClient, encrypted, hydrator, raw

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def principal(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one agent's projection."""
    return journal_store.principal("agent@general")


def _reader(principal: PrincipalStore, client: FakeClient) -> ConversationReader:
    return ConversationReader(store=principal, hydrator=hydrator(principal, client))


def _relations(principal: PrincipalStore, client: FakeClient) -> RelationLookup:
    return RelationLookup(store=principal, runtime=SimpleNamespace(client=client))  # type: ignore[arg-type]


async def _reply_membership_known(principal: PrincipalStore, client: FakeClient, reply_to: str) -> bool:
    """Return whether a plain reply to ``reply_to`` has a known thread membership, which a send requires."""
    return await event_thread_membership_known(
        _reader(principal, client),
        _relations(principal, client),
        ROOM,
        event_type="m.room.message",
        content={"body": "hi", "msgtype": "m.text", "m.relates_to": {"m.in_reply_to": {"event_id": reply_to}}},
    )


def _root_with_only_unreadable_replies() -> FakeClient:
    """A readable root whose replies this device has no keys for."""
    return FakeClient(
        events={"$root": raw("$root", "root", ts=1_000)},
        relations={"$root": [encrypted("$reply", ts=2_000, relates_to={"rel_type": "m.thread", "event_id": "$root"})]},
        olm=object(),
    )


class TestIncompleteHistoryIsNotAbsence:
    """Replies this bot could not read may be the thread, so they cannot be ruled out."""

    async def test_a_tool_cannot_call_a_root_with_unreadable_replies_unthreaded(
        self,
        principal: PrincipalStore,
    ) -> None:
        """Normalizing such a root fails closed instead of resolving to no thread."""
        client = _root_with_only_unreadable_replies()

        root_id = await resolve_thread_root_event_id(
            _reader(principal, client),
            _relations(principal, client),
            ROOM,
            "$root",
        )

        assert root_id is None
        history = await complete_thread_history(_reader(principal, client), ROOM, "$root")
        assert not history.is_full_history

    async def test_a_send_replying_to_that_root_is_refused(self, principal: PrincipalStore) -> None:
        """Thread membership is reported unknown, which the send refuses, rather than room level."""
        client = _root_with_only_unreadable_replies()

        assert not await _reply_membership_known(principal, client, "$root")

    @pytest.mark.parametrize(
        ("absence_needs_full_history", "expected"),
        [(True, ThreadResolutionState.INDETERMINATE), (False, ThreadResolutionState.ROOM_LEVEL)],
    )
    async def test_the_caller_decides_what_an_incomplete_empty_thread_proves(
        self,
        absence_needs_full_history: bool,
        expected: ThreadResolutionState,
    ) -> None:
        """A turn answers at room level; a tool refuses. Complete empty history disproves for both."""
        root_only = raw("$root", "root")

        async def event_info(_room_id: str, _event_id: str) -> EventInfo:
            return EventInfo.from_event(root_only)

        async def no_thread(_room_id: str, _event_id: str) -> None:
            return None

        async def incomplete(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
            return thread_history_result([], is_full_history=False)

        async def complete(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
            return thread_history_result([], is_full_history=True)

        for fetch, state in ((incomplete, expected), (complete, ThreadResolutionState.ROOM_LEVEL)):
            access = thread_messages_thread_membership_access(
                lookup_thread_id=no_thread,
                fetch_event_info=event_info,
                fetch_thread_messages=fetch,
                absence_needs_full_history=absence_needs_full_history,
            )
            resolution = await resolve_related_event_thread_membership(ROOM, "$root", access=access)
            assert resolution.state is state


class _LaterReplyClient(FakeClient):
    """A homeserver that gains one more threaded reply after the first walk."""

    def add_reply(self, source: dict[str, Any]) -> None:
        self.events[source["event_id"]] = source
        self.relations.setdefault("$root", []).append(source)


class _DepartsDuringWalks(FakeClient):
    """A homeserver read during which this bot leaves the room, every time."""

    principal: PrincipalStore | None = None

    async def room_get_event_relations(self, **kwargs: Any) -> AsyncIterator[nio.Event]:  # type: ignore[override]  # noqa: ANN401
        assert self.principal is not None
        await admit_room_membership(self.principal, ROOM, "leave")
        await admit_room_membership(self.principal, ROOM, "join")
        async for event in super().room_get_event_relations(**kwargs):
            yield event


class TestRefetchedThreadHistory:
    """A caller that must see another runtime's write walks the thread again."""

    async def test_a_reply_the_projection_has_not_synced_is_seen_after_a_rewalk(
        self,
        principal: PrincipalStore,
    ) -> None:
        """The marker from the first walk does not stop the second one."""
        root = raw("$root", "root", ts=1_000)
        client = _LaterReplyClient(events={"$root": root}, relations={"$root": []})
        reader = _reader(principal, client)
        assert [message.event_id for message in await complete_thread_history(reader, ROOM, "$root")] == ["$root"]

        client.add_reply(raw("$pin", "pinned summary", ts=2_000, thread_id="$root"))

        stale = await complete_thread_history(reader, ROOM, "$root")
        fresh = await refetched_thread_history(reader, ROOM, "$root", unreadable_is_missing=False)
        assert [message.event_id for message in stale] == ["$root"]
        assert [message.event_id for message in fresh] == ["$root", "$pin"]

    async def test_a_rewalk_that_membership_keeps_refusing_raises(self, principal: PrincipalStore) -> None:
        """An install the current membership refused is not a fresh read, so the caller is told."""
        await admit_room_membership(principal, ROOM, "join")
        client = _DepartsDuringWalks(
            events={"$root": raw("$root", "root", ts=1_000)},
            relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
        )
        client.principal = principal

        with pytest.raises(ConversationReadError, match="rewalk kept losing its membership epoch"):
            await hydrator(principal, client).rewalk(room_id=ROOM, thread_id="$root", unreadable_is_missing=True)


async def test_a_rewalk_that_could_not_read_everything_is_full_history_only_when_unreadable_is_absent(
    principal: PrincipalStore,
) -> None:
    """The marker keeps the widest walk ever recorded; the fresh read reports its own walk.

    A caller proving absence counts the undecryptable reply as missing; one
    that reads only what it can see reads past it to the end of the thread.
    """
    client = _LaterReplyClient(
        events={"$root": raw("$root", "root", ts=1_000)},
        relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
        olm=object(),
    )
    reader = _reader(principal, client)
    assert (await complete_thread_history(reader, ROOM, "$root")).is_full_history

    client.add_reply(encrypted("$pin", ts=3_000, relates_to={"rel_type": "m.thread", "event_id": "$root"}))

    assert (await complete_thread_history(reader, ROOM, "$root")).is_full_history
    assert not (await refetched_thread_history(reader, ROOM, "$root", unreadable_is_missing=True)).is_full_history
    assert (await refetched_thread_history(reader, ROOM, "$root", unreadable_is_missing=False)).is_full_history


def _thread_with_replies(count: int) -> FakeClient:
    replies = [raw(f"$reply{index}", f"reply {index}", ts=2_000 + index, thread_id="$root") for index in range(count)]
    return FakeClient(events={"$root": raw("$root", "root", ts=1_000)}, relations={"$root": replies})


def _thread_with_an_edited_reply(edits: int) -> FakeClient:
    """A reply whose edits only its direct lookup returns, as a recursive walk may page them out."""
    reply = raw("$reply", "reply", ts=2_000, thread_id="$root")
    client = FakeClient(events={"$root": raw("$root", "root", ts=1_000), "$reply": reply}, relations={"$root": [reply]})
    client.relations["$reply"] = [
        raw(f"$edit{index}", f"edit {index}", ts=3_000 + index, replaces="$reply") for index in range(edits)
    ]
    return client


class _WalkDropsRootEdits(FakeClient):
    """A server whose recursive walk leaves out the root's edits while its direct-edit lookup returns them."""

    async def room_get_event_relations(self, **kwargs: Any) -> AsyncIterator[nio.Event]:  # type: ignore[override]  # noqa: ANN401
        async for event in super().room_get_event_relations(**kwargs):
            replaces_root = event.source["content"].get("m.relates_to") == {
                "rel_type": "m.replace",
                "event_id": "$root",
            }
            if kwargs.get("rel_type") is None and replaces_root:
                continue
            yield event


def _thread_whose_walk_drops_root_edits(edits: int) -> FakeClient:
    """An undecryptable reply keeps the walk short of whole, so the root's edits get their own lookup."""
    reply = encrypted("$reply", ts=2_000, relates_to={"rel_type": "m.thread", "event_id": "$root"})
    root_edits = [raw(f"$edit{index}", f"root {index}", ts=3_000 + index, replaces="$root") for index in range(edits)]
    return _WalkDropsRootEdits(
        events={"$root": raw("$root", "root", ts=1_000), "$reply": reply},
        relations={"$root": [reply, *root_edits]},
    )


@pytest.mark.parametrize(
    ("make_client", "bounds", "edits_of"),
    [
        (partial(_thread_with_replies, 3), {"prompt_window_messages": 2}, ()),
        (partial(_thread_with_replies, 3), {"max_fetched_events": 2}, ()),
        (partial(_thread_with_replies, 2), {"max_requests": 1}, ("$reply0", "$reply1")),
        (partial(_thread_with_an_edited_reply, 3), {"max_fetched_events": 2}, ("$reply",)),
        (partial(_thread_whose_walk_drops_root_edits, 3), {"max_fetched_events": 2}, ()),
    ],
    ids=["prompt-window", "event-ceiling", "edit-lookup-requests", "edit-lookup-event-ceiling", "root-edit-lookup"],
)
async def test_a_rewalk_a_bound_stopped_is_not_full_history_even_when_unreadable_is_absent(
    principal: PrincipalStore,
    make_client: Callable[[], FakeClient],
    bounds: dict[str, int],
    edits_of: tuple[str, ...],
) -> None:
    """A walk that a bound ended before the thread did has not read past it, whatever it could decrypt."""
    reader = ConversationReader(store=principal, hydrator=hydrator(principal, make_client(), **bounds))

    refetched = await refetched_thread_history(reader, ROOM, "$root", edits_of=edits_of, unreadable_is_missing=False)

    assert not refetched.is_full_history


async def test_a_rewalk_asked_for_a_message_s_edits_sees_one_the_recursive_walk_paged_out(
    principal: PrincipalStore,
) -> None:
    """A server may drop a reply's edit from the recursive walk; the direct lookup still returns it."""
    summary = raw("$summary", "old title", ts=2_000, thread_id="$root")
    client = FakeClient(
        events={"$root": raw("$root", "root", ts=1_000), "$summary": summary},
        relations={"$root": [summary]},
    )
    reader = _reader(principal, client)
    assert [message.body for message in await complete_thread_history(reader, ROOM, "$root")] == ["root", "old title"]

    # Stored only under the summary, so the recursive walk of the root never returns it.
    client.relations["$summary"] = [raw("$edit", "pinned title", ts=3_000, replaces="$summary")]

    plain = await refetched_thread_history(reader, ROOM, "$root", unreadable_is_missing=False)
    asked = await refetched_thread_history(
        reader,
        ROOM,
        "$root",
        edits_of=["$summary"],
        unreadable_is_missing=False,
    )

    assert [message.body for message in plain] == ["root", "old title"]
    assert [message.body for message in asked] == ["root", "pinned title"]


async def _truncate_room_recovery(principal: PrincipalStore, client: FakeClient, thread_id: str) -> None:
    """Leave the room with a recovery that stopped at its ceiling, and confirm the projection now reports it."""
    recovery = await record_room_history_recovery(principal, ROOM)
    assert recovery is not None
    await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=False,
        unreadable=False,
        attempted_policy_rank=1,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )
    assert not (await complete_thread_history(_reader(principal, client), ROOM, thread_id)).is_full_history


class TestRoomRecoveryThatStoppedShort:
    """A room-wide recovery ceiling says nothing about one thread's own relation walk."""

    async def test_a_redaction_of_a_plain_message_is_still_allowed(self, principal: PrincipalStore) -> None:
        """The plain message's own walk is whole, so it is proven to have no thread."""
        client = FakeClient(events={"$plain": raw("$plain", "plain", ts=1_000)})
        await _truncate_room_recovery(principal, client, "$plain")

        known = await redaction_thread_membership_known(
            _reader(principal, client),
            _relations(principal, client),
            ROOM,
            event_id="$plain",
        )

        assert known is True

    async def test_a_reply_to_a_plain_message_is_still_allowed(self, principal: PrincipalStore) -> None:
        """A send replying to a message nobody threaded from is room level, not unknown."""
        client = FakeClient(events={"$plain": raw("$plain", "plain", ts=1_000)})
        await _truncate_room_recovery(principal, client, "$plain")

        assert await _reply_membership_known(principal, client, "$plain") is True

    async def test_a_root_whose_replies_cannot_be_read_is_still_refused(self, principal: PrincipalStore) -> None:
        """The thread's own walk is incomplete, so its absence of readable replies proves nothing."""
        client = _root_with_only_unreadable_replies()
        await _truncate_room_recovery(principal, client, "$root")

        assert await _reply_membership_known(principal, client, "$root") is False
