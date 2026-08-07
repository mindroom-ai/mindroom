"""One departure fences once, however many times it is reported.

Against the real store on both backends, because the property being proven is
that the debt and the invalidation it pairs with commit together. A fake that
records intentions cannot fail the way a half-applied departure fails.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    DepartureOutcome,
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    MembershipFence,
    ProjectedEvent,
)

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ALICE = "@alice:example.org"


@dataclass
class RecordingStore:
    """The real store, recording which rooms it actually invalidated.

    ``fails_next_fence`` models a durable advance that never commits, which is
    the case the process-local marker got wrong: it recorded the debt first, so
    a failed advance left the departure with no fence at all.

    ``fails_next_owed_report_read`` and ``fails_next_retirement`` model the two
    other durable operations the same marker discipline applies to: the read
    that recovers inherited debt, and the write that retires it.
    """

    principal: PrincipalStore
    advanced: list[str] = field(default_factory=list)
    fails_next_fence: BaseException | None = None
    fails_next_owed_report_read: BaseException | None = None
    fails_next_retirement: BaseException | None = None

    async def fence_departure(self, room_id: str, *, source: DepartureSource) -> DepartureOutcome:
        """Apply one departure observation, recording the ones that invalidated."""
        failure = self.fails_next_fence
        if failure is not None:
            self.fails_next_fence = None
            raise failure
        outcome = await self.principal.fence_departure(room_id, source=source)
        if outcome.fenced:
            self.advanced.append(room_id)
        return outcome

    async def note_membership_restarted(self, room_id: str) -> None:
        """Record a confirmed join."""
        await self.principal.note_membership_restarted(room_id)

    async def retire_owed_departure_reports(self, room_id: str) -> None:
        """Forget reports that can no longer arrive."""
        failure = self.fails_next_retirement
        if failure is not None:
            self.fails_next_retirement = None
            raise failure
        await self.principal.retire_owed_departure_reports(room_id)

    async def rooms_owing_departure_reports(self) -> frozenset[str]:
        """Return rooms whose local departure is still owed a report."""
        failure = self.fails_next_owed_report_read
        if failure is not None:
            self.fails_next_owed_report_read = None
            raise failure
        return await self.principal.rooms_owing_departure_reports()


@pytest.fixture
def principal(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


@pytest.fixture
def store(principal: PrincipalStore) -> RecordingStore:
    """Return the recording view of one principal's departure bookkeeping."""
    return RecordingStore(principal=principal)


@pytest.fixture
def membership(store: RecordingStore) -> MembershipFence:
    """Return a fence over the real store."""
    return MembershipFence(store=store)


async def sync_response_without_departures(membership: MembershipFence) -> None:
    """Apply one ordinary sync response, which reports no departure at all."""
    await membership.fence_reported_departures([])


async def test_a_local_departure_fences_immediately(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A local departure fences immediately, without waiting for sync."""
    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM]
    assert await principal.membership_epoch(ROOM) == 1


async def test_a_sync_reported_departure_fences(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A departure the bot did not initiate still fences."""
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]
    assert await principal.membership_epoch(ROOM) == 1


async def test_the_echo_of_a_local_departure_does_not_fence_again(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """The sync report of a local leave is the same departure, not a second one."""
    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_second_departure_after_the_echo_fences_again(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Absorbing one echo must not deafen the fence to the next real departure.

    A second departure needs a rejoin in front of it. Without one the bot is
    already out of the room, so a second report of a leave is the first report
    arriving twice.
    """
    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM])
    await membership.note_membership_restarted(ROOM)
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_a_replayed_report_does_not_fence_a_second_time(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A sync report is not a once-only signal, so it cannot be trusted as one.

    A checkpoint that could not advance re-presents the response that carried
    the leave, and a backfilled limited timeline can carry the leave event
    again on its own. The bot cannot leave a room it has not rejoined, so the
    repeat is the same departure and owes no second invalidation: one
    departure, one epoch, whichever observer saw it.
    """
    await membership.fence_reported_departures([ROOM])

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]
    assert await principal.membership_epoch(ROOM) == 1


async def test_an_echo_absorbs_only_its_own_room(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """An echo absorbs only its own room."""
    await membership.fence_local_departure(ROOM)
    await membership.fence_reported_departures([ROOM, OTHER_ROOM])

    assert store.advanced == [ROOM, OTHER_ROOM]


async def test_no_departures_asks_for_nothing(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """An ordinary sync response fences nothing."""
    await sync_response_without_departures(membership)

    assert store.advanced == []


async def test_a_rejoin_before_the_echo_keeps_its_projection(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """The echo must not delete a conversation hydrated under the new membership."""
    await membership.fence_local_departure(ROOM)
    await membership.note_membership_restarted(ROOM)
    await admit_message(principal, "$after-rejoin", body="hello again")

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]
    assert await visible_bodies(principal) == ["hello again"]


async def test_a_rejoin_before_the_echo_keeps_its_queued_answer(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """An answer queued under the new membership survives the previous one's echo."""
    from mindroom.event_journal import DeliveryStage  # noqa: PLC0415 - one call needs the enum

    await membership.fence_local_departure(ROOM)
    await membership.note_membership_restarted(ROOM)
    await principal.enqueue_delivery(
        turn_id="$after-rejoin",
        stage=DeliveryStage.FINAL,
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
    )

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]
    delivery = await principal.load_delivery(turn_id="$after-rejoin", stage=DeliveryStage.FINAL)
    assert delivery is not None


async def test_a_failed_advance_leaves_the_departure_to_its_report(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """A departure whose advance never committed still owes its one fence.

    Nothing may record "a report is owed" for an invalidation that did not
    happen: the report would then be absorbed, and the departure would end up
    with no fence at all -- stale state surviving into the new membership.
    """
    store.fails_next_fence = RuntimeError("durable advance failed")
    with pytest.raises(RuntimeError, match="durable advance failed"):
        await membership.fence_local_departure(ROOM)

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_cancelled_advance_leaves_the_departure_to_its_report(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Cancellation is the same as failure: nothing committed, nothing owed."""
    store.fails_next_fence = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await membership.fence_local_departure(ROOM)

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_restart_between_the_fence_and_its_report_absorbs_the_report(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """The debt outlives the process that took it on."""
    await membership.fence_local_departure(ROOM)

    restarted = MembershipFence(store=store)
    await restarted.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_two_local_departures_before_either_report_fence_twice(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Two memberships ended, so two invalidations, and then two reports absorbed.

    One bit cannot say this. It says "a report is owed", the first report
    clears it, and the second fences a membership it did not end.
    """
    await membership.fence_local_departure(ROOM)
    await membership.note_membership_restarted(ROOM)
    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM, ROOM]

    await membership.fence_reported_departures([ROOM])
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_a_local_departure_repeated_without_a_rejoin_fences_once(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """A bot cannot leave a room it is not in, so the second call is the same departure."""
    await membership.fence_local_departure(ROOM)
    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM]


async def test_a_report_that_never_arrives_stops_absorbing_departures(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """A leave whose report was collapsed away must not swallow the next departure.

    A leave and a rejoin inside one gappy timeline produce no separate report
    for the leave, so a debt kept forever would absorb the room's next genuine
    departure instead -- and the state built under a membership that really did
    end would survive into the one after it.
    """
    await membership.fence_local_departure(ROOM)

    await sync_response_without_departures(membership)
    await sync_response_without_departures(membership)
    await membership.note_membership_restarted(ROOM)

    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_an_owed_report_is_still_absorbed_inside_its_window(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Retirement must not be so eager that it retires a report still in flight.

    The sync response processed immediately after a local departure may have
    been generated before the leave landed, so it proves nothing about whether
    the report is coming.
    """
    await membership.fence_local_departure(ROOM)

    await sync_response_without_departures(membership)
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM]


async def test_a_retired_report_is_forgotten_durably(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A restart must not resurrect a report that was already given up on.

    Proven through the next genuine departure, because that is what a
    resurrected debt would swallow. The rejoin is what makes the report after
    it a second departure rather than the first one arriving twice.
    """
    await membership.fence_local_departure(ROOM)
    await sync_response_without_departures(membership)
    await sync_response_without_departures(membership)

    assert await principal.rooms_owing_departure_reports() == frozenset()

    restarted = MembershipFence(store=store)
    await restarted.note_membership_restarted(ROOM)
    await restarted.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_a_failed_recovery_of_inherited_debt_is_tried_again(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A store read that failed recovered nothing, so recovery is still owed.

    Debt inherited from a previous process is only ever retired by the window
    this recovery hands it. Treating a failed read as a completed one loses
    that window for the life of the process, and the debt it left behind goes
    on to absorb the room's next genuine departure.
    """
    await membership.fence_local_departure(ROOM)

    restarted = MembershipFence(store=store)
    store.fails_next_owed_report_read = RuntimeError("durable read failed")
    with pytest.raises(RuntimeError, match="durable read failed"):
        await sync_response_without_departures(restarted)

    await sync_response_without_departures(restarted)
    await sync_response_without_departures(restarted)
    assert await principal.rooms_owing_departure_reports() == frozenset()

    await restarted.note_membership_restarted(ROOM)
    await restarted.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_a_failed_retirement_is_tried_again(
    membership: MembershipFence,
    store: RecordingStore,
    principal: PrincipalStore,
) -> None:
    """A retirement that never committed leaves the debt, so its window must stay.

    Spending the window on a write that failed retires nothing and leaves
    nothing that ever will, so the debt outlives the departure it belongs to
    and swallows the next one.
    """
    await membership.fence_local_departure(ROOM)
    await sync_response_without_departures(membership)

    store.fails_next_retirement = RuntimeError("durable retirement failed")
    with pytest.raises(RuntimeError, match="durable retirement failed"):
        await sync_response_without_departures(membership)

    await sync_response_without_departures(membership)
    assert await principal.rooms_owing_departure_reports() == frozenset()

    await membership.note_membership_restarted(ROOM)
    await membership.fence_reported_departures([ROOM])

    assert store.advanced == [ROOM, ROOM]


async def test_a_concurrent_local_and_reported_departure_fence_once(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Both observers of one departure racing must still produce one invalidation."""
    await asyncio.gather(
        membership.fence_local_departure(ROOM),
        membership.fence_reported_departures([ROOM]),
    )

    assert store.advanced == [ROOM]


async def test_a_departure_reported_before_the_local_one_fences_once(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """A sync response can report a leave before the leaving code gets to fence it."""
    await membership.fence_reported_departures([ROOM])
    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM]


async def test_a_departure_after_a_reported_one_and_a_rejoin_fences_again(
    membership: MembershipFence,
    store: RecordingStore,
) -> None:
    """Suppressing the local half of one departure must not suppress the next."""
    await membership.fence_reported_departures([ROOM])
    await membership.fence_local_departure(ROOM)
    await membership.note_membership_restarted(ROOM)
    await membership.fence_local_departure(ROOM)

    assert store.advanced == [ROOM, ROOM]


async def admit_message(principal: PrincipalStore, event_id: str, *, body: str) -> None:
    """Admit one visible room message under the room's current membership."""
    content: dict[str, object] = {"msgtype": "m.text", "body": body}
    await principal.admit(
        InboundEvent(
            event_id=event_id,
            room_id=ROOM,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=ALICE,
            origin_server_ts=1_000,
            source={"content": content},
        ),
        ProjectedEvent(
            event_id=event_id,
            room_id=ROOM,
            thread_id=None,
            sender=ALICE,
            origin_server_ts=1_000,
            content=content,
            replaces_event_id=None,
            redacts_event_id=None,
        ),
    )


async def visible_bodies(principal: PrincipalStore) -> list[str]:
    """Return the bodies the room's conversation currently shows."""
    page = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)
    return [str(message.content["body"]) for message in page.messages]
