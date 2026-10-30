"""What the projection may conclude about a thread it cannot fully see, and how a caller sees a fresh write."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.matrix.conversation_hydration import _HydrationError
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
from mindroom.matrix.thread_mutation_impact import event_thread_membership_known
from tests.journal_membership_helpers import admit_room_membership
from tests.test_conversation_hydration import ROOM, FakeClient, encrypted, hydrator, raw

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

        known = await event_thread_membership_known(
            _reader(principal, client),
            _relations(principal, client),
            ROOM,
            event_type="m.room.message",
            content={"body": "hi", "msgtype": "m.text", "m.relates_to": {"m.in_reply_to": {"event_id": "$root"}}},
        )

        assert not known

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
        fresh = await refetched_thread_history(reader, ROOM, "$root")
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

        with pytest.raises(_HydrationError, match="rewalk kept losing its membership epoch"):
            await hydrator(principal, client).rewalk(room_id=ROOM, thread_id="$root")


async def test_a_rewalk_that_could_not_read_everything_is_not_full_history_despite_an_older_complete_marker(
    principal: PrincipalStore,
) -> None:
    """The marker keeps the widest walk ever recorded; the fresh read reports its own walk."""
    client = _LaterReplyClient(
        events={"$root": raw("$root", "root", ts=1_000)},
        relations={"$root": [raw("$reply", "reply", ts=2_000, thread_id="$root")]},
        olm=object(),
    )
    reader = _reader(principal, client)
    assert (await complete_thread_history(reader, ROOM, "$root")).is_full_history

    client.add_reply(encrypted("$pin", ts=3_000, relates_to={"rel_type": "m.thread", "event_id": "$root"}))

    assert (await complete_thread_history(reader, ROOM, "$root")).is_full_history
    assert not (await refetched_thread_history(reader, ROOM, "$root")).is_full_history
