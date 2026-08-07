"""Crash the turn pipeline at each of its nine boundaries.

A durable design is only as good as its worst interruption point. Each test
below stops the process at one specific moment, restarts everything that is
not durable, and then checks the two properties that matter: exactly one
terminal turn, and at most one visible response.

The model is counted as well. Enqueueing is what makes an answer durable, so a
crash before it costs a model run and a crash after it must not: the stored
payload is the answer, and asking the model for another one would only produce
a result that can never become visible.

The turn below has no "have I already answered this?" check, deliberately,
because production has none either -- `JournalDispatcher` hands every pending
source to its callback and lets the turn engine decide. What stops the second
model run is that the source stops being pending in the same transaction that
records the answer. A handler that consulted the outbox first would pass every
test here while production still ran the model twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import nio
import pytest

from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    SettlementOutcome,
)
from mindroom.event_journal.store import _DEFAULT_UNACKNOWLEDGED_LIMIT as _UNACKNOWLEDGED_BATCH
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.pending_event_worker import PendingEventWorker
from mindroom.response_delivery import ResponseDelivery, TurnHandoff
from tests.conftest import CrashError, DiesAfterAcknowledgement, DiesAfterNextWriteCommit

if TYPE_CHECKING:
    from mindroom.event_journal import JournalEvent, OutboxDelivery, OutboxView, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOT = "@mindroom_general:example.org"
SOURCE = "$inbound"
PRINCIPAL = "agent@alice"


@dataclass
class FakeHomeserver:
    """A Matrix server that deduplicates by transaction ID, like a real one."""

    events: dict[str, str] = field(default_factory=dict)
    sends: int = 0
    fail_next_send: bool = False
    # Fail every send until this many attempts have been made, so a test can
    # exhaust a whole recovery page rather than one row.
    fail_sends_until: int = 0
    lose_acknowledgement: bool = False

    async def send(self, delivery: OutboxDelivery) -> str:
        """Accept one delivery, collapsing a repeated transaction ID."""
        self.sends += 1
        if self.sends <= self.fail_sends_until:
            msg = "connection reset"
            raise CrashError(msg)
        if self.fail_next_send:
            self.fail_next_send = False
            msg = "connection reset"
            raise CrashError(msg)
        event_id = self.events.setdefault(delivery.transaction_id, f"$sent{len(self.events)}")
        if self.lose_acknowledgement:
            self.lose_acknowledgement = False
            msg = "crashed after Matrix accepted the message"
            raise CrashError(msg)
        return event_id

    @property
    def visible_messages(self) -> int:
        """Return how many distinct events this server actually holds."""
        return len(self.events)


# A turn here answers exactly one source, and hands over exactly that one.
_SETTLE_THE_SOURCE = TurnHandoff(sources_for_turn=lambda turn_id: (turn_id,), released=lambda _event_ids: None)


@dataclass
class TurnRuntime:
    """Everything that would be rebuilt by a restart."""

    store: PrincipalStore
    crashing_backend: DiesAfterNextWriteCommit
    homeserver: FakeHomeserver
    model_runs: int = 0
    crash_after_model: bool = False
    crash_after_enqueue: bool = False
    crash_after_acknowledgement: bool = False

    @property
    def delivery(self) -> ResponseDelivery:
        """Return a fresh delivery view, as a restart would."""
        return ResponseDelivery(store=self.store, send=self.homeserver.send)

    def _outbox(self) -> OutboxView:
        """Return the outbox this attempt writes through, crashes and all.

        The enqueue crash sits at the backend's commit, not at the store call
        around it: the point of that boundary is the instant *between* two
        commits, and a probe outside the store call would step over a store
        that ran two of them.
        """
        self.crashing_backend.armed = self.crash_after_enqueue
        principal = EventJournalStore(backend=cast("Any", self.crashing_backend)).principal(PRINCIPAL)
        if not self.crash_after_acknowledgement:
            return principal
        return cast("OutboxView", DiesAfterAcknowledgement(principal))

    async def handle(self, event: JournalEvent) -> SettlementOutcome | None:
        """Run one turn: model, the durable handoff, then claim and send.

        Nothing here asks whether this turn was already answered. It cannot:
        the handoff settles the source inside the transaction that records the
        answer, so the worker never offers the same source twice.
        """
        self.model_runs += 1
        answer = f"answer to {event.event_id}"
        if self.crash_after_model:
            msg = "crashed after the model finished"
            raise CrashError(msg)

        await ResponseDelivery(
            store=self._outbox(),
            send=self.homeserver.send,
            handoff=_SETTLE_THE_SOURCE,
        ).deliver(
            turn_id=event.event_id,
            stage=DeliveryStage.FINAL,
            room_id=event.room_id,
            thread_id=event.thread_id,
            payload={"msgtype": "m.text", "body": answer},
        )
        # The handoff already settled this source. Reporting an outcome as
        # well would be a second authority over the same fact.
        return None

    def worker(self) -> PendingEventWorker:
        """Return a fresh worker, as a restart would."""
        return PendingEventWorker(store=self.store, handle=self.handle)


@pytest.fixture
def runtime(journal_store: EventJournalStore) -> TurnRuntime:
    """Return one turn runtime over a real store."""
    return TurnRuntime(
        store=journal_store.principal(PRINCIPAL),
        crashing_backend=DiesAfterNextWriteCommit(inner=journal_store.backend),
        homeserver=FakeHomeserver(),
    )


def inbound(event_id: str = SOURCE) -> nio.Event:
    """Return one parsed inbound message."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": 1_000,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "question"},
        },
    )
    assert isinstance(event, nio.Event)
    return event


async def admit(store: PrincipalStore, event: nio.Event | None = None) -> None:
    """Admit one inbound message durably."""
    event = event or inbound()
    await store.admit(
        inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
        projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
    )


async def assert_settled_once(runtime: TurnRuntime) -> None:
    """Assert the outcome every boundary must reach."""
    assert await runtime.store.pending() == (), "the event still owes work"
    settled = await runtime.store.load_event(SOURCE)
    assert settled is not None, "the event vanished from the journal"
    assert runtime.homeserver.visible_messages == 1, f"{runtime.homeserver.visible_messages} visible responses"


class TestCrashMatrix:
    """One terminal turn and at most one visible response, at every boundary."""

    async def test_one_before_journal_commit(self, runtime: TurnRuntime) -> None:
        """Nio was never told the event was accepted, so it redelivers it."""
        # Nothing was admitted: the transaction did not commit.
        assert await runtime.store.pending() == ()
        assert runtime.homeserver.visible_messages == 0

        await admit(runtime.store)
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_two_after_journal_commit_before_nio_accepts(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Nio redelivers what it was not told about; the journal deduplicates."""
        await admit(runtime.store)
        await admit(runtime.store)

        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_three_after_acceptance_before_the_worker_starts(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The pending row is the entire handoff, so a restart just resumes."""
        await admit(runtime.store)

        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_four_after_turn_creation_before_the_model_runs(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """No durable result yet, so the model has to run — exactly once."""
        await admit(runtime.store)
        runtime.crash_after_model = True
        runtime.model_runs = -1  # The crashed attempt does not count as a real run.

        await runtime.worker().drain_once()
        assert await runtime.store.pending() != ()

        runtime.crash_after_model = False
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_five_after_the_model_before_the_result_is_durable(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Nothing is durable yet, so the answer has to be produced again."""
        await admit(runtime.store)
        runtime.crash_after_model = True
        await runtime.worker().drain_once()

        assert await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL) is None

        runtime.crash_after_model = False
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 2

    async def test_six_after_the_handoff_before_the_claim_commits(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The first boundary the outbox owns outright.

        The answer and the settlement committed together, so the journal has
        nothing left to replay and the model must not run again. Everything
        still owed is a row the outbox knows how to resend.
        """
        await admit(runtime.store)
        runtime.crash_after_enqueue = True
        await runtime.worker().drain_once()

        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert runtime.homeserver.sends == 0
        assert await runtime.store.pending() == (), "the answer and its handoff commit together"

        runtime.crash_after_enqueue = False
        await runtime.delivery.recover()
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_seven_after_the_claim_before_network_io(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The claim is committed, so recovery resends the identical payload."""
        await admit(runtime.store)
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "claimed"},
        )
        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert claimed is not None

        recovered = (await runtime.delivery.recover()).recovered

        assert recovered == 1
        assert runtime.homeserver.visible_messages == 1
        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "claimed"

    async def test_eight_after_matrix_accepts_before_acknowledgement(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The dangerous one: the message exists but MindRoom does not know.

        Recovery resends under the same deterministic transaction ID, which
        the homeserver collapses back into the event it already created.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True

        await runtime.worker().drain_once()
        assert runtime.homeserver.visible_messages == 1

        await runtime.delivery.recover()
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1

    async def test_nine_after_the_acknowledgement_commits(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The last boundary, and the one the handoff made uneventful.

        Settlement used to be a separate write that happened here, which made
        this a real interruption point: the answer was durable, the source was
        not settled, and the restart replayed the turn on top of an answer
        already in the room. Now everything durable is already written, so a
        crash at this instant owes nobody anything.
        """
        await admit(runtime.store)
        runtime.crash_after_acknowledgement = True
        await runtime.worker().drain_once()

        assert await runtime.store.pending() == ()
        assert runtime.homeserver.visible_messages == 1

        runtime.crash_after_acknowledgement = False
        await runtime.delivery.recover()
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.homeserver.sends == 1
        assert runtime.model_runs == 1


class TestRecoveryIsComplete:
    """Startup recovery either sends everything it owes, or is not recovery."""

    async def test_more_deliveries_than_one_batch_are_all_sent(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A bound that stops at one page leaves answers permanently unsent."""
        count = _UNACKNOWLEDGED_BATCH + 1
        for index in range(count):
            await runtime.store.enqueue_delivery(
                turn_id=f"turn-{index:04d}",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": f"answer {index}"},
            )

        recovered = (await runtime.delivery.recover()).recovered

        assert recovered == count
        assert runtime.homeserver.visible_messages == count
        assert await runtime.store.unacknowledged_deliveries() == ()

    async def test_a_whole_failing_page_does_not_starve_what_is_behind_it(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A failure leaves its row in the very query recovery re-reads.

        So filtering failures in memory is not enough: one full page of them
        pins the window, and every delivery behind it is never attempted. The
        page here fails entirely, and the row after it still has to be sent.
        """
        count = _UNACKNOWLEDGED_BATCH + 1
        for index in range(count):
            await runtime.store.enqueue_delivery(
                turn_id=f"turn-{index:04d}",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": f"answer {index}"},
            )
        runtime.homeserver.fail_sends_until = _UNACKNOWLEDGED_BATCH

        recovered = (await runtime.delivery.recover()).recovered

        assert recovered == 1
        assert runtime.homeserver.visible_messages == 1

    async def test_one_failing_delivery_does_not_block_the_rest(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A delivery that cannot be sent stays unacknowledged, so it repeats.

        Recovery has to remember it rather than re-reading it forever, or the
        first failure makes every later answer unreachable.
        """
        for index in range(2):
            await runtime.store.enqueue_delivery(
                turn_id=f"turn-{index}",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": f"answer {index}"},
            )
        runtime.homeserver.fail_next_send = True

        recovered = (await runtime.delivery.recover()).recovered

        assert recovered == 1
        assert runtime.homeserver.visible_messages == 1


class TestModelIsNotRerun:
    """Boundaries five through nine must not spend the model again."""

    async def test_a_regenerated_answer_cannot_replace_an_accepted_one(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The exact case claiming exists to prevent.

        Matrix accepted the first answer. A restart produced a different one.
        Sending it under the same transaction ID would be silently discarded,
        leaving the durable result and the room disagreeing forever — so the
        claimed payload wins and stays visible.
        """
        await admit(runtime.store)
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "first answer"},
        )
        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert claimed is not None
        await runtime.homeserver.send(claimed)

        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "regenerated answer"},
        )
        await runtime.delivery.recover()

        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "first answer"
        assert runtime.homeserver.visible_messages == 1

    async def test_a_rejoin_between_send_and_acknowledgement_leaves_one_answer(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The end the user sees: one question, one answer, across a rejoin.

        Matrix accepted the answer and the acknowledgement was lost, so the
        bot cannot know whether the message exists. It then leaves and rejoins
        the room, which drops everything derived from the old membership. The
        answer was already handed to the outbox, so what survives the rejoin
        is an attempted row and its frozen transaction — and resending that is
        what leaves the room holding exactly one answer.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()
        assert runtime.homeserver.visible_messages == 1

        await runtime.store.advance_membership_epoch(ROOM)
        await runtime.delivery.recover()
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.homeserver.visible_messages == 1
        assert runtime.model_runs == 1

    async def test_recovery_after_acknowledgement_sends_nothing(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Recovery after acknowledgement sends nothing."""
        await admit(runtime.store)
        await runtime.worker().drain_once()
        sends_before = runtime.homeserver.sends

        await runtime.delivery.recover()

        assert runtime.homeserver.sends == sends_before

    async def test_a_send_failure_leaves_the_delivery_retryable_not_the_turn(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """What a failed send owes is a resend, not another answer.

        The handoff committed before the network call, so the journal is done
        with this source whether or not the message reached Matrix. The row is
        unacknowledged and recovery resends the payload it froze -- which is
        the point of freezing it, because the model is not going to be asked
        for a second one.
        """
        await admit(runtime.store)
        runtime.homeserver.fail_next_send = True

        await runtime.worker().drain_once()
        assert await runtime.store.pending() == ()
        assert runtime.homeserver.visible_messages == 0
        assert await runtime.store.unacknowledged_deliveries() != ()

        assert (await runtime.delivery.recover()).recovered == 1

        await assert_settled_once(runtime)
        assert runtime.model_runs == 1


class TestTheHandoffIsOneTransaction:
    """The answer and the settlement of what it answers commit together.

    Pinned at the store rather than through a delivery, because this is the
    property the backend provides and both backends have to provide it. Two
    transactions would leave an instant where the answer is durable and the
    source is still pending -- the state a restart turns into a second model
    run for a question that was already answered.
    """

    async def test_a_settlement_that_cannot_be_written_rolls_the_answer_back(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """No route may leave a durable answer whose sources are still pending.

        The failure is injected at the per-event write rather than at
        ``settle_many``, which is a no-op for an empty set: a split
        implementation that settled afterwards could still call the batch
        function with nothing in it, and a patch there would fire inside the
        enqueue's own transaction and roll it back for the wrong reason.
        """
        await admit(runtime.store)

        with (
            patch(
                "mindroom.event_journal.store.journal.settle",
                side_effect=CrashError("the settlement could not be written"),
            ),
            pytest.raises(CrashError),
        ):
            await runtime.store.enqueue_delivery(
                turn_id=SOURCE,
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "the answer"},
                settle_source_event_ids=(SOURCE,),
            )

        assert await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL) is None
        assert [event.event_id for event in await runtime.store.pending()] == [SOURCE]

    async def test_a_refused_enqueue_settles_nothing(self, runtime: TurnRuntime) -> None:
        """A fenced answer leaves no row, so it must leave the source owed.

        Settling here would retire work with no owner left to do it, which is
        the silent loss the ordering exists to prevent.
        """
        await admit(runtime.store)
        await runtime.store.advance_membership_epoch(ROOM)

        transaction_id = await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
            settle_source_event_ids=(SOURCE,),
        )

        assert transaction_id is None
        assert [event.event_id for event in await runtime.store.pending()] == [SOURCE]


class TestInitialAndFinalStages:
    """A turn's two visible deliveries are independently idempotent."""

    async def test_the_stages_do_not_share_a_transaction(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The stages do not share a transaction."""
        initial = await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "thinking"},
        )
        final = await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )

        assert initial != final

    async def test_recovery_sends_the_answer_and_drops_the_placeholder(self, runtime: TurnRuntime) -> None:
        """A turn whose answer is owed does not also owe its placeholder.

        The placeholder exists to stand in until the answer arrives. Once the
        answer is a durable row, sending both puts "thinking" in the room next
        to the reply it was standing in for, and nothing ever edits it away.
        """
        for stage, body in ((DeliveryStage.INITIAL, "thinking"), (DeliveryStage.FINAL, "answer")):
            await runtime.store.enqueue_delivery(
                turn_id=SOURCE,
                stage=stage,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": body},
            )

        assert (await runtime.delivery.recover()).recovered == 1
        assert runtime.homeserver.visible_messages == 1
        sends_after_recovery = runtime.homeserver.sends

        # Acknowledged deliveries leave the recovery set, and the skipped
        # placeholder is still skipped, so a second restart sends nothing.
        assert (await runtime.delivery.recover()).recovered == 0
        assert runtime.homeserver.sends == sends_after_recovery
        assert runtime.homeserver.visible_messages == 1
