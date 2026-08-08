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

from mindroom.event_journal import (
    DepartureSource,
    EventClass,
    EventKind,
    HistoryDebtOutcome,
    HydrationPolicy,
    RoomHistoryDebt,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_hydration import ConversationHydrator, _HydrationError
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.matrix.sync_certification import SyncRecoveryOutcome, SyncTrustState
from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust
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
_STORE_GENERATION = "room-history-debt"
_STUCK = "s_stuck"  # an opaque Matrix sync token, not a credential
_SETTLED_LOG = "conversation_history_debt_settled"
_RECORDED_LOG = "matrix_sync_recovery_gap_recorded_as_history_debt"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def raw(
    event_id: str,
    body: str,
    *,
    ts: int,
    thread_id: str | None = None,
    replaces: str | None = None,
) -> dict[str, Any]:
    """Return one raw Matrix message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if replaces is not None:
        content["m.new_content"] = {"msgtype": "m.text", "body": body}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": content,
    }


def redaction(event_id: str, redacts: str, *, ts: int) -> dict[str, Any]:
    """Return one raw Matrix redaction of another event."""
    return {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "type": "m.room.redaction",
        "redacts": redacts,
        "content": {},
    }


def redacted(source: dict[str, Any]) -> dict[str, Any]:
    """Return the stripped shape a server serves for an event that was redacted."""
    return {
        **source,
        "content": {},
        "unsigned": {"redacted_because": {"type": "m.room.redaction", "sender": ALICE, "content": {}}},
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


def hydrator(
    store: PrincipalStore,
    client: FakeClient,
    *,
    require_complete: bool = False,
    policy: HydrationPolicy = HydrationPolicy.PROMPT,
    **bounds: int,
) -> ConversationHydrator:
    """Return a hydrator wired to a fake homeserver.

    The bounds are shrunk far below either policy's real ceilings so a walk can
    reach one inside a test. The policy is passed separately for that reason:
    it names which caller this stands for, and the durable marker is compared
    on that ordering rather than on the shrunken numbers.
    """
    return ConversationHydrator(
        store=store,
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
        require_complete=require_complete,
        policy=policy,
        **bounds,
    )


def trust_over(tmp_path: Path, store: PrincipalStore) -> SyncCheckpointTrust:
    """Build real checkpoint trust that records its skipped gaps in a real journal."""
    return SyncCheckpointTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        logger=get_logger(),
        state=SyncTrustState.PENDING,
        store_generation=_STORE_GENERATION,
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
    trust: SyncCheckpointTrust,
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
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
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
    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(
        room_id=ROOM,
        owed_through_ts=2_000,
        owed_through_event_id="$two",
    )

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


async def test_repaid_gap_messages_are_readable_but_answer_nobody(alice: PrincipalStore) -> None:
    """The deliberate limit of the escape, pinned so it cannot be mistaken for a bug.

    Repayment restores the *conversation*, not the *work*. The messages the bot
    never saw become readable, and no turn is ever owed for them, so they are
    never answered.

    This is not a cost the escape introduced. Those events were undispatched
    because nio could not fetch them, which is equally true without the escape
    -- the difference is only that a principal without one stays wedged and
    stops answering every other room too. Repayment strictly adds the reading.

    Nor is leaving them unanswered an oversight, and the reason is contract 11.
    Actionability comes
    from nio's provenance and MindRoom may never infer it. These events did not
    arrive through sync at all -- they were fetched by a client-initiated
    ``/messages`` walk, which carries no ``TimelineEventProvenance`` -- so
    admitting them as actionable would mean inventing the classification that
    contract forbids. Every alternative is a heuristic about how stale a gap is
    allowed to be before its messages stop deserving an answer, and none of
    them can be derived from anything the server said.

    So the walk installs projected events and admits nothing, which is exactly
    what cold history does, and is measured the same way: zero pending events.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)

    client = FakeClient(
        pages=[
            [
                raw("$three", "three", ts=3_000),
                raw("$two", "two", ts=2_000),
                raw("$one", "one", ts=1_000),
            ],
        ],
    )
    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    # The gap's messages are readable...
    assert await bodies(alice) == ["one", "two", "three"]
    assert await alice.room_history_debt(ROOM) is None
    # ...and owe no reply. Asserted against the live event rather than against
    # an empty set, because an empty set would also pass if admission had
    # stopped working entirely. `$one` arrived through sync and is pending
    # exactly as it should be; `$two` and `$three` came back from the
    # repayment walk and are absent, so no turn will ever run for them.
    assert await alice.unsettled_event_ids() == frozenset({"$one"})


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


async def test_a_reader_holding_a_settled_debt_does_not_walk_the_room_again(
    alice: PrincipalStore,
) -> None:
    """A repaid hole is repaid for every reader, including the ones already holding it.

    The sibling above races three readers and so only catches this when the
    scheduler happens to cooperate -- which is what made it flaky rather than
    green. This one reproduces the losing ordering directly: ``_shared`` joins
    only readers that overlap in time, so a reader that read this debt before
    another reader's repayment committed finds nothing left to join and walks
    the entire room a second time. The install is refused, so every request that
    walk costs is spent on an answer that is thrown away.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    debt = await alice.record_room_history_debt(ROOM)
    assert debt is not None
    client = FakeClient(pages=[[raw("$two", "two", ts=2_000), raw("$one", "one", ts=1_000)]])
    hydrate = hydrator(alice, client)

    await hydrate.ensure_hydrated(room_id=ROOM, thread_id=None)
    assert client.calls == 2

    await hydrate._repay(debt)

    assert client.calls == 2
    assert await bodies(alice) == ["one", "two"]


async def test_reach_is_measured_over_what_the_walk_saw_not_what_it_kept(
    alice: PrincipalStore,
) -> None:
    """An anchor the projection drops carries the walk just as far back.

    The anchor here comes back redacted, so nothing about it survives
    projection: it was the projection's newest message when the gap was skipped
    and it has been redacted since. Judging coverage by the projected rows would
    call this walk short and file a repaired room as permanently lost history --
    a walk that reached the anchor's position in the timeline is a walk that
    covered the hole, whatever the event turned out to still contain.
    """
    await admit_all(alice, [raw("$anchor", "anchor", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [
                raw("$new", "new", ts=3_000),
                raw("$mid", "mid", ts=2_000),
                redacted(raw("$anchor", "anchor", ts=1_000)),
            ],
        ],
    )

    with capture_logs() as logs:
        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    assert settled[0]["saw_anchor"]
    assert settled[0]["reached_ts"] == 1_000


async def test_a_clock_skewed_event_at_the_tip_does_not_discharge_the_debt(
    alice: PrincipalStore,
) -> None:
    """Coverage is a position in the room's history, not a reading of a clock.

    ``origin_server_ts`` is the sending server's clock and nothing makes it
    agree with the order the homeserver paginates in. Federated skew and a
    bridge that rewrites timestamps both put an event older than its neighbours
    at the tip of the timeline, and judging coverage by the oldest timestamp
    seen anywhere lets that one event satisfy the whole debt on the first page.

    The walk then stops with its window full, never asks for the pages the gap
    is actually on, and the loss is filed as a repayment and logged at info as
    success. Only reaching the anchor itself proves the walk went past the hole.
    """
    await admit_all(alice, [raw("$anchor", "anchor", ts=2_000)])
    assert await alice.record_room_history_debt(ROOM) == RoomHistoryDebt(
        room_id=ROOM,
        owed_through_ts=2_000,
        owed_through_event_id="$anchor",
    )
    client = FakeClient(
        pages=[
            [
                raw("$new", "new", ts=5_000),
                # A tip event whose clock reads older than the anchor's.
                raw("$skewed", "skewed", ts=900),
                raw("$also", "also", ts=4_000),
            ],
            [raw("$gap", "gap", ts=3_000), raw("$anchor", "anchor", ts=2_000)],
        ],
    )

    with capture_logs() as logs:
        await hydrator(alice, client, prompt_window_messages=3).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    # The second page is the one the anchor and the gap are on, and the walk has
    # to ask for it: a repayment that stops on the first page is the defect.
    assert client.calls == 2
    assert await bodies(alice) == ["skewed", "anchor", "gap", "also", "new"]


async def test_an_event_sharing_the_anchor_timestamp_is_not_the_anchor(
    alice: PrincipalStore,
) -> None:
    """Two events can carry the same millisecond, and only one of them is owed.

    Accepting equality discharges the debt against whichever event happens to
    share the anchor's clock reading, which says nothing about whether the walk
    reached the anchor's own position in the timeline.
    """
    await admit_all(alice, [raw("$anchor", "anchor", ts=2_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [raw("$new", "new", ts=5_000), raw("$twin", "twin", ts=2_000)],
            [raw("$gap", "gap", ts=3_000), raw("$anchor", "anchor", ts=2_000)],
        ],
    )

    with capture_logs() as logs:
        await hydrator(alice, client, prompt_window_messages=2).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    assert client.calls == 2
    assert await bodies(alice) == ["anchor", "twin", "gap", "new"]


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
            """Leave and rejoin before answering, as a racing rejoin would."""
            await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
            await alice.note_membership_restarted(ROOM)
            return await super().room_messages(*args, **kwargs)

    client = _RejoiningClient(pages=[[raw("$two", "two", ts=2_000)]])

    # This server rejoins on every request, so no attempt can ever install. A
    # real rejoin happens once and the retry succeeds; a room that never settles
    # is one the bot cannot get a stable view of, and the read fails visibly
    # rather than handing back a page with nothing in it. Returning quietly here
    # is what let a strict prompt treat an unhydrated conversation as whole.
    with capture_logs() as logs, pytest.raises(_HydrationError, match="membership epoch"):
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


# --- A hole can contain deletions as well as messages ------------------------


async def test_a_redaction_inside_the_gap_removes_what_it_deleted(alice: PrincipalStore) -> None:
    """A repayment that only installs messages leaves deleted content readable.

    Sync was wedged while a user deleted a message, so the redaction is inside
    the hole and the original it deleted was projected before the hole opened.
    A walk that keeps only ``m.room.message`` events fetches that redaction and
    throws it away, installs nothing that removes the original, and then settles
    the debt -- declaring the room whole with the deleted body still in the
    projection, for the life of the database. Hydration does not run twice under
    one membership, so nothing later would have repaired it either.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000), raw("$two", "two", ts=2_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [
                redaction("$r", "$one", ts=3_000),
                raw("$two", "two", ts=2_000),
                redacted(raw("$one", "one", ts=1_000)),
            ],
        ],
    )

    with capture_logs() as logs:
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    settled = [entry for entry in logs if entry["event"] == _SETTLED_LOG]
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.REPAID.value]
    assert await bodies(alice) == ["two"]
    assert await alice.room_history_debt(ROOM) is None


async def test_a_redaction_inside_the_gap_hides_the_edit_it_deleted(alice: PrincipalStore) -> None:
    """Deleting the revision on screen is not deleting the message.

    The logical message survives its edit being redacted, so the row stays and
    its body has to stop being readable until the server says what the message
    looks like now -- which is exactly what live admission does, and what the
    refresh token is for. A repayment that drops the redaction leaves the row
    showing the edit the sender deleted and owes no refetch, so no later read
    ever asks.
    """
    await admit_all(alice, [raw("$m", "first", ts=1_000), raw("$e", "second", ts=2_000, replaces="$m")])
    assert await bodies(alice) == ["second"]
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(
        pages=[
            [
                redaction("$r", "$e", ts=3_000),
                redacted(raw("$e", "second", ts=2_000, replaces="$m")),
                raw("$m", "first", ts=1_000),
            ],
        ],
    )

    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
    assert [message.content["body"] for message in page.messages] == []
    # Withheld and owed, rather than silently gone: the point refetch is what
    # learns whether an older revision survived the deletion.
    assert [request.logical_event_id for request in page.refresh_pending] == ["$m"]
    assert await alice.room_history_debt(ROOM) is None


# --- What the debt refuses to do --------------------------------------------


async def test_a_rejoin_drops_a_debt_for_the_membership_that_owed_it(alice: PrincipalStore) -> None:
    """A hole between messages a rejoin just deleted is not a hole at all."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)

    await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

    assert await alice.room_history_debt(ROOM) is None


async def test_a_gap_deeper_than_the_cost_ceiling_stops_without_declaring_it_lost(
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
    assert [entry["outcome"] for entry in settled] == [HistoryDebtOutcome.TRUNCATED.value]
    assert settled[0]["log_level"] == "warning"
    assert settled[0]["owed_through_ts"] == 1_000
    # The ceiling is what stopped it and the window is not: a prompt walk would
    # have been satisfied after three messages, and this one spent its whole
    # allowance before admitting the history was out of reach.
    assert walks_after_loss == 5
    assert await alice.room_history_debt(ROOM) is None
    assert client.calls == walks_after_loss
    # The room is short, and says so -- but it is not stamped as having lost
    # history. The ceiling measures this walk's allowance, not what the server
    # still holds: superseded edits are collapsed out of pagination over time,
    # so the same allowance can carry a later walk past an anchor it missed.
    assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
    assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)


async def test_a_failed_repayment_leaves_the_debt_for_the_next_read(alice: PrincipalStore) -> None:
    """An unreachable homeserver degrades a read rather than settling a debt."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(fail=True)

    with pytest.raises(_HydrationError):
        await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(
        room_id=ROOM,
        owed_through_ts=1_000,
        owed_through_event_id="$one",
    )
    assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_an_empty_projection_owes_nothing(alice: PrincipalStore) -> None:
    """A skip leaves a hole only between stored history and what arrives next.

    With nothing stored there is no hole, just a conversation that starts later
    -- which is what every unread room looks like, and no debt is invented for
    it because there is no stored message an anchor could name.
    """
    assert await alice.record_room_history_debt(ROOM) is None
    assert await alice.room_history_debt(ROOM) is None


async def test_an_empty_projection_drops_the_marker_that_certified_the_hole(
    alice: PrincipalStore,
) -> None:
    """An empty projection is not proof that the room has nothing to miss.

    "A first hydration walk fills it in anyway" is the justification for
    recording no debt, and the rooms this actually happens to are the ones where
    that walk has already run. A room the homeserver holds real history for can
    project nothing at all -- undecryptable events, redactions, reactions, state
    -- and the walk that found only those is complete over zero visible
    messages, which is a warm marker.

    Recording nothing there let the checkpoint advance past the gap while the
    marker kept answering for it: no debt withholds the marker, so the next
    strict read serves a conversation missing everything sent during the skip
    without making a single server request, and reports it as whole.

    Still no debt -- there is no stored message to anchor one on -- but the
    marker that certified the hole is dropped, so the next read walks the room.
    """
    gone = redacted(raw("$gone", "gone", ts=1_000))
    warm = FakeClient(pages=[[gone]])
    await hydrator(alice, warm).ensure_hydrated(room_id=ROOM, thread_id=None)

    # The dangerous state: a whole conversation with nothing in it.
    assert await bodies(alice) == []
    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)

    assert await alice.record_room_history_debt(ROOM) is None
    assert await alice.room_history_debt(ROOM) is None

    # One re-walk, rather than an unbounded tax on a room that owes nothing.
    assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    repair = FakeClient(pages=[[raw("$missed", "missed", ts=3_000), gone]])
    await hydrator(alice, repair).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await bodies(alice) == ["missed"]
    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_a_second_skip_keeps_the_older_hole(alice: PrincipalStore) -> None:
    """Two gaps are one hole to a reader, and it starts at the earlier of them."""
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    first = await alice.record_room_history_debt(ROOM)
    await admit_all(alice, [raw("$two", "two", ts=5_000)])
    second = await alice.record_room_history_debt(ROOM)

    older = RoomHistoryDebt(room_id=ROOM, owed_through_ts=1_000, owed_through_event_id="$one")
    assert first == older
    assert second == older


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


async def test_lost_history_does_not_make_a_strict_caller_walk_the_room_forever(
    alice: PrincipalStore,
) -> None:
    """A caller that needs completeness must still stop asking for the impossible.

    Lost history is the one truncation no further walk can repair: the hole is
    behind what the server still holds, so the room answers "not complete"
    forever. A strict caller re-walking on that answer would spend its whole
    epoch-retry budget on full room walks and then fail with the wrong error.
    One walk, then the honest record, and the caller refuses on that.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    client = FakeClient(pages=[[raw("$six", "six", ts=6_000), raw("$five", "five", ts=5_000)]])
    strict = hydrator(alice, client, require_complete=True, policy=HydrationPolicy.EXPORT)

    await strict.ensure_hydrated(room_id=ROOM, thread_id=None)
    walked = client.calls
    await strict.ensure_hydrated(room_id=ROOM, thread_id=None)

    assert client.calls == walked
    assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    # The two questions that must not be conflated. The walk did reach the end
    # of what the server still holds, which is why walking again is pointless;
    # the room is still not whole, which is why a strict caller still refuses.
    coverage = await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None)
    assert coverage is not None
    assert coverage.reached_its_end
    assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_a_repayment_counts_as_the_deeper_walk_on_later_reads_too(
    alice: PrincipalStore,
) -> None:
    """The mirror of the case above, for a repayment that stopped at a ceiling.

    A repayment satisfies the window as well as the debt, so it is the deeper
    walk a strict caller was owed, and the loop that launched it has always
    treated it that way. Only the current call knew, though: nothing durable
    said which bound had been spent, so every later read of the same room
    walked the whole thing again and reached the same ceiling. It has to hold
    across calls, because a strict caller builds a new hydrator every time.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])
    await alice.record_room_history_debt(ROOM)
    # A room deeper than the walk's request ceiling, so the repayment stops
    # short of both the anchor and the start of the room.
    client = FakeClient(endless=True)
    bounds: dict[str, Any] = {
        "prompt_window_messages": 1,
        "max_requests": 3,
        "require_complete": True,
        "policy": HydrationPolicy.EXPORT,
    }

    await hydrator(alice, client, **bounds).ensure_hydrated(room_id=ROOM, thread_id=None)
    walked = client.calls
    assert walked == 3

    await hydrator(alice, client, **bounds).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert client.calls == walked
    # Not re-walked, and still honestly short: a strict caller refuses on this.
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
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)

    class _RefusingRecorder:
        async def record_room_history_debt(self, room_id: str) -> RoomHistoryDebt | None:
            msg = f"cannot record history debt for {room_id}"
            raise OSError(msg)

    trust = SyncCheckpointTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        logger=get_logger(),
        state=SyncTrustState.PENDING,
        store_generation=_STORE_GENERATION,
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
    save_sync_token(tmp_path, "code", _STUCK, store_generation=_STORE_GENERATION)
    trust = trust_over(tmp_path, alice)
    assert await trust.prepare_startup() == _STUCK

    with capture_logs() as logs:
        await stall_until_skipped(trust, room_ids=frozenset({ROOM, OTHER_ROOM}))

    recorded = [(entry["room_id"], entry["owed_through_ts"]) for entry in logs if entry["event"] == _RECORDED_LOG]
    assert recorded == [(OTHER_ROOM, None), (ROOM, 1_000)]
    assert await alice.room_history_debt(ROOM) == RoomHistoryDebt(
        room_id=ROOM,
        owed_through_ts=1_000,
        owed_through_event_id="$one",
    )
    assert await alice.room_history_debt(OTHER_ROOM) is None


async def test_a_walk_carrying_a_settled_debt_changes_nothing(alice: PrincipalStore) -> None:
    """A repayment whose debt another walk already settled must not land.

    Two readers of an indebted room can each be holding a debt snapshot: one
    reads it, a second reads it before the first settles, and the shared-task
    map cannot be joined to a walk that has already finished. The loser then
    arrives with an answer to a question nobody is asking any more.

    Neither of its effects is harmless. Its shorter walk would replace the room
    conversation the winner installed in full, and ``settle``'s loss branch is
    sticky -- it would stamp permanent history loss on a room that was repaid
    seconds earlier, which makes every later completeness question answer no and
    fails every thread export for that membership, forever, with no outstanding
    debt left to send any read back to the server.
    """
    await admit_all(alice, [raw("$old", "old", ts=1_000)])
    debt = await alice.record_room_history_debt(ROOM)
    assert debt is not None

    client = FakeClient(pages=[[raw("$gap", "gap", ts=1_000), raw("$old", "old", ts=1_000)]])
    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)
    assert await alice.room_history_debt(ROOM) is None
    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)

    # The loser: same debt object, a walk that never reaches the anchor.
    outcome = await alice.repay_room_history_debt(
        debt,
        events=(),
        complete=False,
        saw_anchor=False,
        expected_membership_epoch=await alice.membership_epoch(ROOM),
    )

    assert outcome is HistoryDebtOutcome.SUPERSEDED
    # The repaid room is untouched: not lost, still complete, still readable.
    assert not await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)
    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
    assert await bodies(alice) == ["gap", "old"]


async def test_a_single_rejoin_during_hydration_retries_under_the_new_epoch(
    alice: PrincipalStore,
) -> None:
    """One membership change mid-walk must not leave the conversation unhydrated.

    The shared task is keyed by conversation, not by epoch, so a reader that
    arrives in the new membership joins the old membership's walk and is handed
    its result -- a result whose install was refused. Reporting success there is
    what let a strict prompt read an empty page and call it whole, because a
    missing hydration row is not a truncation.
    """
    await admit_all(alice, [raw("$one", "one", ts=1_000)])

    @dataclass
    class _RejoinsOnce(FakeClient):
        """A homeserver that rejoins under the first walk and settles after.

        Every call is one complete walk, because a real server still holds this
        history when the retry asks for it.
        """

        rejoined: bool = False

        async def room_messages(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - matches the fake it overrides
            """Advance membership under the first request only."""
            del args, kwargs
            self.calls += 1
            if not self.rejoined:
                self.rejoined = True
                await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
                await alice.note_membership_restarted(ROOM)
            chunk = [parse(raw("$two", "two", ts=2_000))]
            return nio.RoomMessagesResponse(ROOM, chunk, "start", None)

    client = _RejoinsOnce()

    await hydrator(alice, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    # The retry ran under the settled epoch and installed, so the caller's
    # promise holds: returning means a marker exists for the current membership.
    assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    assert await bodies(alice) == ["two"]


async def test_a_ceiling_walk_leaves_the_room_repairable(alice: PrincipalStore) -> None:
    """Spending the allowance must not stamp the room as having lost history.

    The two uncovered outcomes are not the same statement. Exhaustion says the
    server no longer holds it; the ceiling says this walk could not afford it.
    Only the first is permanent, and the difference is load-bearing because
    ``history_lost`` is sticky per room: once set, no later walk can make the
    room complete again, however far back it reaches.
    """
    await admit_all(alice, [raw("$old", "old", ts=1_000)])
    await alice.record_room_history_debt(ROOM)

    await hydrator(alice, FakeClient(endless=True), prompt_window_messages=3, max_requests=5).ensure_hydrated(
        room_id=ROOM,
        thread_id=None,
    )
    # The debt is cleared, so no later read re-walks the ceiling forever.
    assert await alice.room_history_debt(ROOM) is None

    # A later walk that does reach its anchor completes the room, in the same
    # membership -- a new debt makes an indebted room read as unhydrated, so the
    # next read walks again. Under a sticky loss flag this stays False no matter
    # what the server hands back.
    await admit_all(alice, [raw("$new", "new", ts=5_000)])
    debt = await alice.record_room_history_debt(ROOM)
    assert debt is not None
    reached = raw(debt.owed_through_event_id, "reached", ts=debt.owed_through_ts)
    await hydrator(alice, FakeClient(pages=[[reached]])).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
