"""Liveness without loss when sync gives up on rebuilding a room's gap.

Certification is all-or-nothing per response, so one room nio cannot rebuild
freezes the checkpoint for every other room, and retrying from a checkpoint
that never advances asks for a strictly larger gap each time. The escape from
that livelock is to certify past the gap; what makes the escape honest rather
than a silent deletion is that the skipped history is written down first and
repaid by the next read of the room.

These tests assert that end to end and by value: the checkpoint sequence the
transport produces, and the actual bodies of the messages the bot never saw
appearing in the projection once the debt is repaid.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.event_journal import EventClass, EventKind, HistoryDebtOutcome, RoomHistoryDebt
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_hydration import ConversationHydrator, _HydrationError
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import SyncRecoveryOutcome, SyncTrustState
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_recovery_escape import _CLASSIC_SYNC_RECOVERY_STALL_LIMIT
from tests.sync_continuity_helpers import certify_response, load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!wedged:localhost"
OTHER_ROOM = "!healthy:localhost"
ALICE = "@alice:localhost"
BOT = "@mindroom_general:localhost"
_CACHE_GENERATION = "room-history-debt"
_STUCK = "s_stuck"  # an opaque Matrix sync token, not a credential
_SETTLED_LOG = "conversation_history_debt_settled"
_RECORDED_LOG = "matrix_sync_recovery_gap_recorded_as_history_debt"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def raw(event_id: str, body: str, *, ts: int, thread_id: str | None = None) -> dict[str, Any]:
    """Return one raw Matrix message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": content,
    }


def parse(source: dict[str, Any]) -> nio.Event:
    """Return the parsed nio event for one raw source."""
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


async def admit_all(store: PrincipalStore, sources: list[dict[str, Any]]) -> None:
    """Admit raw events the way live sync would."""
    for source in sources:
        event = parse(source)
        await store.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )


@dataclass
class FakeClient:
    """A homeserver serving one room's history newest first, one page per call."""

    pages: list[list[dict[str, Any]]] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Pages served beyond the last one, which is how a server whose history is
    # deeper than any bounded walk behaves.
    endless: bool = False
    calls: int = 0
    fail: bool = False

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
    ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
        """Return one page of history, oldest page last."""
        del room_id, start, direction, limit
        if self.fail:
            return nio.RoomMessagesError("M_FORBIDDEN")
        index = self.calls
        self.calls += 1
        if index < len(self.pages):
            chunk = [parse(source) for source in self.pages[index]]
            return nio.RoomMessagesResponse(ROOM, chunk, "start", f"end-{index}")
        if self.endless:
            # Each further page is one message older than the last, forever.
            older = raw(f"$filler{index}", f"filler {index}", ts=10_000 - index)
            return nio.RoomMessagesResponse(ROOM, [parse(older)], "start", f"end-{index}")
        return nio.RoomMessagesResponse(ROOM, [], "start", None)

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
        response = nio.RoomGetEventResponse()
        response.event = parse(source)
        return response

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Yield one thread's relations newest first."""
        del room_id, direction, recurse, minimum_recursion_depth
        for source in sorted(
            self.relations.get(event_id, []),
            key=lambda item: item["origin_server_ts"],
            reverse=True,
        ):
            yield parse(source)


def hydrator(store: PrincipalStore, client: FakeClient, **bounds: int) -> ConversationHydrator:
    """Return a hydrator wired to a fake homeserver."""
    return ConversationHydrator(
        store=store,
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
        **bounds,
    )


class _EventCache:
    """Match the production cache startup contract used by cache trust."""

    cache_generation: str = _CACHE_GENERATION

    async def initialize(self) -> None:
        """Match the production cache startup contract."""

    async def purge_principal(self) -> None:
        """Match cold-start principal cleanup."""

    def disable(self, _reason: str) -> None:
        """Match the production cache disable contract."""


def trust_over(tmp_path: Path, store: PrincipalStore) -> SyncCacheTrust:
    """Build real cache trust that records its skipped gaps in a real journal."""
    return SyncCacheTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        runtime=SimpleNamespace(event_cache=_EventCache()),  # type: ignore[arg-type]
        logger=get_logger(),
        state=SyncTrustState.PENDING,
        store_generation=_CACHE_GENERATION,
        history_debt_provider=lambda: store,
    )


def sync_response(*, next_batch: str, unrecovered_room_ids: frozenset[str]) -> nio.SyncResponse:
    """Build a real nio response carrying an authoritative recovery outcome."""
    return nio.SyncResponse(
        next_batch=next_batch,
        rooms=nio.Rooms(invite={}, join={}, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=unrecovered_room_ids,
    )


async def stall_until_skipped(
    trust: SyncCacheTrust,
    *,
    room_ids: frozenset[str] = frozenset({ROOM}),
    first_token: int = 0,
) -> list[str]:
    """Drive one full stall window and return the cursor after each response."""
    tokens: list[str] = []
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT):
        response = sync_response(
            next_batch=f"s_live_{first_token + attempt}",
            unrecovered_room_ids=room_ids,
        )
        await certify_response(
            trust,
            next_batch=response.next_batch,
            recovery=SyncRecoveryOutcome.from_sync_response(response, admission_refused=False),
        )
        tokens.append(trust.retry_token() or "")
    return tokens


async def bodies(store: PrincipalStore, thread_id: str | None = None) -> list[str]:
    """Return the visible bodies of one conversation, oldest first."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return [str(message.content["body"]) for message in page.messages]


# --- The whole trade, end to end --------------------------------------------


async def test_a_skipped_gap_moves_the_checkpoint_and_the_next_read_repays_it(
    alice: PrincipalStore,
    tmp_path: Path,
) -> None:
    """Liveness now, no loss later: the point of the whole mechanism.

    Two messages arrive live, sync then wedges on the room, and while it is
    wedged the room receives two more that never reach admission. The escape
    certifies past them so every other room stays live -- and the debt it wrote
    down is what puts those two messages into the projection on the next read.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000), raw("$two", "two", ts=2_000)])
    save_sync_token(tmp_path, "code", _STUCK, cache_generation=_CACHE_GENERATION)
    trust = trust_over(tmp_path, alice)
    assert await trust.prepare_startup() == _STUCK

    cursors = await stall_until_skipped(trust)

    # Every attempt but the last rewinds to the checkpoint it was measured from;
    # the last certifies past the gap so the principal keeps moving.
    assert cursors == [_STUCK] * (_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1) + [
        f"s_live_{_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}",
    ]
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == f"s_live_{_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1}"
    # The two messages sent during the gap are simply not there yet.
    assert await bodies(alice) == ["one", "two"]
    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(room_id=ROOM, owed_through_ts=2_000)

    # The server still has them, and the next read is what goes and gets them.
    client = FakeClient(
        pages=[
            [
                raw("$four", "four", ts=4_000),
                raw("$three", "three", ts=3_000),
                raw("$two", "two", ts=2_000),
                raw("$one", "one", ts=1_000),
            ],
        ],
    )
    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await bodies(alice) == ["one", "two", "three", "four"]
    assert await alice.room_history_debt(ROOM) is None
    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_an_already_hydrated_room_walks_again_for_its_debt(alice: PrincipalStore) -> None:
    """The shape production actually produces, and the reason the gate exists.

    A long-lived room is hydrated once and kept current by live sync, so when a
    gap is skipped its marker is warm and every later read is served from a
    projection with a hole in it. Nothing about the page says so. Only refusing
    to honor the marker while the room owes history sends the read back to the
    server.
    """
    warm = FakeClient(pages=[[raw("$one", "one", ts=1_000)]])
    await hydrator(alice, warm).ensure_hydrated(room_id=ROOM, thread_id=None)
    assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    await alice.record_room_history_debt(ROOM)

    assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    repair = FakeClient(pages=[[raw("$two", "two", ts=2_000), raw("$one", "one", ts=1_000)]])
    await hydrator(alice, repair).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await bodies(alice) == ["one", "two"]
    assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_a_repaid_room_never_walks_the_server_again(alice: PrincipalStore) -> None:
    """The debt gates exactly one walk, not a walk on every read.

    One walk and not two: the repayment walks the room, so a room read that
    triggered it must not then walk the same pages again to install the same
    rows. The count is asserted by value because a second walk is invisible in
    the projection it produces.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(pages=[[raw("$two", "two", ts=2_000), raw("$one", "one", ts=1_000)]])
    hydrate = hydrator(alice, client)

    await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)
    calls_after_repayment = client.calls
    await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)
    await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await bodies(alice) == ["one", "two"]
    # One page of history and the empty page that proves exhaustion, once.
    assert calls_after_repayment == 2
    assert client.calls == calls_after_repayment


async def test_concurrent_readers_of_an_indebted_room_share_one_walk(
    alice: PrincipalStore,
) -> None:
    """One hole, one repayment, however many conversations are waiting on it.

    Every conversation in an indebted room reads as unhydrated, so a busy room
    can have several readers arrive at the repayment at once. Each running its
    own room walk would multiply one outage into a burst of full-history
    pagination against a homeserver that was already struggling.
    """
    await admit_all(alice, [raw("$root", "root", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[[raw("$two", "two", ts=2_000, thread_id="$root"), raw("$root", "root", ts=1_000)]],
        events={"$root": raw("$root", "root", ts=1_000)},
        relations={"$root": []},
    )
    hydrate = hydrator(alice, client)

    await asyncio.gather(
        hydrate.ensure_hydrated(room_id=ROOM, thread_id=None),
        hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root"),
        hydrate.ensure_hydrated(room_id=ROOM, thread_id="$root"),
    )

    # One page and the empty page that proves exhaustion, walked once between
    # all three readers.
    assert client.calls == 2
    assert await bodies(alice, "$root") == ["root", "two"]


async def test_reach_is_measured_over_what_the_walk_saw_not_what_it_kept(
    alice: PrincipalStore,
) -> None:
    """A page of events the projection drops carries the walk just as far back.

    The oldest event here is redacted, so nothing about it survives projection.
    Judging coverage by the projected rows would call this walk short and file
    a repaired room as lost history.
    """
    await admit_all(alice, [raw("$anchor", "anchor", ts=2_000)])
    await alice.record_room_history_debt(ROOM)
    redacted = raw("$gone", "gone", ts=1_000)
    redacted["content"] = {}
    redacted["unsigned"] = {"redacted_because": {"type": "m.room.redaction", "sender": ALICE, "content": {}}}
    client = FakeClient(
        pages=[[raw("$new", "new", ts=3_000), raw("$anchor", "anchor", ts=2_000), redacted]],
    )

    with capture_logs() as logs:
        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    assert settled[0]["reached_ts"] == 1_000


async def test_a_repayment_walks_past_the_prompt_window_to_reach_its_anchor(
    alice: PrincipalStore,
) -> None:
    """The prompt window bounds a prompt, and a repayment is not one.

    A room with more recent history than a prompt reads is the ordinary case,
    not a pathology: the anchor sits behind messages the window fills up on
    first. A walk that stops the moment the window is full reaches a timestamp
    newer than the one it owes, so it files as lost history the homeserver is
    still holding on the very next page -- and loss is sticky, so that one short
    walk answers every later completeness question about the room with no.
    """
    await admit_all(alice, [raw("$anchor", "anchor", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [
                raw("$five", "five", ts=5_000),
                raw("$four", "four", ts=4_000),
                raw("$three", "three", ts=3_000),
            ],
            [raw("$two", "two", ts=2_000), raw("$anchor", "anchor", ts=1_000)],
        ],
    )

    with capture_logs() as logs:
        await hydrator(alice, client, prompt_window_messages=3).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    assert settled[0]["reached_ts"] == 1_000
    # The page the window-bounded walk never asked for, and the gap's own
    # messages that were sitting on it the whole time.
    assert await bodies(alice) == ["anchor", "two", "three", "four", "five"]


async def test_a_rejoin_during_the_repayment_walk_installs_nothing(alice: PrincipalStore) -> None:
    """A view of the previous membership must not be written into the new one."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    debt = await alice.record_room_history_debt(ROOM)
    assert debt is not None

    @dataclass
    class _RejoiningClient(FakeClient):
        """A homeserver whose answer arrives after the bot left and rejoined."""

        async def room_messages(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - matches the fake it overrides
            """Advance membership before answering, as a racing rejoin would."""
            await alice.advance_membership_epoch(ROOM)
            return await super().room_messages(*args, **kwargs)

    client = _RejoiningClient(pages=[[raw("$two", "two", ts=2_000)]])

    with capture_logs() as logs:
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    # The walk reached past the timestamp it owed, so it would have settled --
    # but it is a view of a membership that has ended, and reporting it as a
    # repayment or as lost history would both be claims about a conversation
    # that no longer exists.
    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.SUPERSEDED.value]
    # The rejoin dropped the projection, the marker, and the debt together, and
    # the in-flight walk added nothing back to any of them.
    assert await bodies(alice) == []
    assert await alice.room_history_debt(ROOM) is None
    assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_reading_a_thread_repays_the_whole_room(alice: PrincipalStore) -> None:
    """One room walk repairs every conversation the hole touched.

    A thread's relation tree says what that thread contains, never what the room
    received while sync was wedged, so the repayment is always the room walk --
    whichever conversation the reader actually asked for.
    """
    await admit_all(alice, [raw("$root", "root", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [
                raw("$missed", "missed reply", ts=3_000, thread_id="$root"),
                raw("$root", "root", ts=1_000),
            ],
        ],
        events={"$root": raw("$root", "root", ts=1_000)},
        relations={"$root": []},
    )

    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id="$root")

    # The threaded message nobody asked the server for by thread is present,
    # because the room walk is what fetched it.
    assert await bodies(alice, "$root") == ["root", "missed reply"]
    assert await alice.room_history_debt(ROOM) is None


# --- What the debt refuses to do --------------------------------------------


async def test_a_rejoin_drops_a_debt_for_the_membership_that_owed_it(alice: PrincipalStore) -> None:
    """A hole between messages a rejoin just deleted is not a hole at all."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)

    await alice.advance_membership_epoch(ROOM)

    assert await alice.room_history_debt(ROOM) is None


async def test_a_gap_deeper_than_the_cost_ceiling_records_the_loss_and_stops(
    alice: PrincipalStore,
) -> None:
    """The one bound that does stop a repayment, and it has to say so.

    The ceiling measures what a walk costs rather than what a prompt reads, so
    unlike the window it is a bound a repayment has to answer to. A gap behind
    it cannot be repaired by waiting either: the next walk starts from a tip
    that has only moved forward, so the same allowance carries it less far back,
    never further. Retrying on every read forever would be worse than the freeze
    this replaced, so the loss is recorded once, the gate lifts, and every
    completeness question about the room answers no.
    """
    await admit_all(alice, [raw("$old", "old", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    # A server whose history outlives any bounded walk, all of it newer than the
    # timestamp the debt owes.
    client = FakeClient(endless=True)
    hydrate = hydrator(alice, client, prompt_window_messages=3, max_requests=5)

    with capture_logs() as logs:
        await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)
    walks_after_loss = client.calls
    await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.LOST.value]
    assert settled[0]["log_level"] == "error"
    assert settled[0]["owed_through_ts"] == 1_000
    # The ceiling is what stopped it and the window is not: a prompt walk would
    # have been satisfied after three messages, and this one spent its whole
    # allowance before admitting the history was out of reach.
    assert walks_after_loss == 5
    assert await alice.room_history_debt(ROOM) is None
    assert client.calls == walks_after_loss
    # Lost history is not truncation in front of the page; it is a hole behind
    # it, and a caller told the page was whole would report a length that never
    # existed.
    assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
    assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)


async def test_a_failed_repayment_leaves_the_debt_for_the_next_read(alice: PrincipalStore) -> None:
    """An unreachable homeserver degrades a read rather than settling a debt."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(fail=True)

    with pytest.raises(_HydrationError):
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(room_id=ROOM, owed_through_ts=1_000)
    assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_an_empty_projection_owes_nothing(alice: PrincipalStore) -> None:
    """A skip leaves a hole only between stored history and what arrives next.

    With nothing stored there is no hole, just a conversation that starts later
    -- which is what every unread room looks like and what the first hydration
    walk fills in anyway.
    """
    assert await alice.record_room_history_debt(ROOM) is None
    assert await alice.room_history_debt(ROOM) is None


async def test_a_second_skip_keeps_the_older_hole(alice: PrincipalStore) -> None:
    """Two gaps are one hole to a reader, and it starts at the earlier of them."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    first = await alice.record_room_history_debt(ROOM)
    await admit_all(alice, [raw("$two", "two", ts=5_000)])
    second = await alice.record_room_history_debt(ROOM)

    assert first == RoomHistoryDebt(room_id=ROOM, owed_through_ts=1_000)
    assert second == RoomHistoryDebt(room_id=ROOM, owed_through_ts=1_000)


async def test_a_walk_that_runs_out_of_server_history_still_owns_up_to_the_hole(
    alice: PrincipalStore,
) -> None:
    """Running out of room is not the same statement as covering the debt.

    A server that has purged the history a debt names answers a walk that
    reaches the very beginning of what it still holds and never gets near the
    timestamp. That walk is complete, and it is also exactly the case where
    history really was lost, so completeness has to keep answering no long after
    the walk that proved it finished.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    # Everything the server still holds is newer than the message the projection
    # is anchored on, and the walk exhausts it.
    client = FakeClient(pages=[[raw("$six", "six", ts=6_000), raw("$five", "five", ts=5_000)]])

    with capture_logs() as logs:
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [(entry["outcome"], entry["walk_complete"]) for entry in settled] == [
        (HistoryDebtOutcome.LOST.value, True),
    ]
    assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
    assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)


# --- Ordering against the checkpoint ----------------------------------------


async def test_the_debt_is_durable_before_the_checkpoint_that_skips_it(
    alice: PrincipalStore,
    tmp_path: Path,
) -> None:
    """A crash between the two orderings must never be able to lose history.

    Recording first costs one redundant walk if the checkpoint never lands.
    Certifying first moves the watermark past history nothing is left to ask
    for, so a recorder that cannot write has to stop the checkpoint outright.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    save_sync_token(tmp_path, "code", _STUCK, cache_generation=_CACHE_GENERATION)

    class _RefusingRecorder:
        async def record_room_history_debt(self, room_id: str) -> RoomHistoryDebt | None:
            msg = f"cannot record history debt for {room_id}"
            raise OSError(msg)

    trust = SyncCacheTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        runtime=SimpleNamespace(event_cache=_EventCache()),  # type: ignore[arg-type]
        logger=get_logger(),
        state=SyncTrustState.PENDING,
        store_generation=_CACHE_GENERATION,
        history_debt_provider=_RefusingRecorder,
    )
    assert await trust.prepare_startup() == _STUCK

    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT - 1):
        response = sync_response(next_batch=f"s_live_{attempt}", unrecovered_room_ids=frozenset({ROOM}))
        await certify_response(
            trust,
            next_batch=response.next_batch,
            recovery=SyncRecoveryOutcome.from_sync_response(response, admission_refused=False),
        )
    skipping = sync_response(next_batch="s_live_skip", unrecovered_room_ids=frozenset({ROOM}))

    with pytest.raises(OSError, match="cannot record history debt"):
        await certify_response(
            trust,
            next_batch=skipping.next_batch,
            recovery=SyncRecoveryOutcome.from_sync_response(skipping, admission_refused=False),
        )

    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == _STUCK


async def test_every_skipped_room_is_written_down_and_only_a_holed_one_owes(
    alice: PrincipalStore,
    tmp_path: Path,
) -> None:
    """Two rooms skipped by one checkpoint are two separate accounts of loss.

    Both are named, because the operator has to see everything the watermark
    moved past. Only the one with stored history owes a walk: a hole is a gap
    between what is already projected and what arrives next, and a room nothing
    has ever been read from has no first half to be disconnected from.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    save_sync_token(tmp_path, "code", _STUCK, cache_generation=_CACHE_GENERATION)
    trust = trust_over(tmp_path, alice)
    assert await trust.prepare_startup() == _STUCK

    with capture_logs() as logs:
        await stall_until_skipped(trust, room_ids=frozenset({ROOM, OTHER_ROOM}))

    recorded = [(entry["room_id"], entry["owed_through_ts"]) for entry in logs if entry["event"] == _RECORDED_LOG]
    assert recorded == [(OTHER_ROOM, None), (ROOM, 1_000)]
    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(room_id=ROOM, owed_through_ts=1_000)
    assert await alice.room_history_debt(OTHER_ROOM) is None
