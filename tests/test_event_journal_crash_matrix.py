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

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import nio
import pytest

from mindroom.event_journal import (
    DeliveryStage,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
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
    """A Matrix server that deduplicates by transaction ID, like a real one.

    Like a real one means the idempotency key includes the *device*. A Matrix
    transaction ID is unique per device rather than per user, so the same ID
    replayed after a re-login is one this server has never seen and it accepts
    the message again. A fake that deduplicated on the ID alone would be kinder
    than any homeserver, and would pass a resend that duplicates an answer in
    production.
    """

    # (device, transaction id) -> the event that pair produced.
    events: dict[tuple[str, str], str] = field(default_factory=dict)
    # What the room holds, as (turn, event). This is what a history scan reads,
    # and the only place a duplicated answer is visible.
    room_events: list[tuple[str, str]] = field(default_factory=list)
    accepted_stages: list[DeliveryStage] = field(default_factory=list)
    # Changing this is a re-login: a new device, and an empty transaction-ID
    # namespace to go with it.
    device_id: str = "DEVICE1"
    sends: int = 0
    room_scans: int = 0
    fail_next_send: bool = False
    # Fail every send until this many attempts have been made, so a test can
    # exhaust a whole recovery page rather than one row.
    fail_sends_until: int = 0
    lose_acknowledgement: bool = False

    async def send(self, delivery: OutboxDelivery) -> str:
        """Accept one delivery, collapsing a transaction ID this device reused."""
        self.sends += 1
        if self.sends <= self.fail_sends_until:
            msg = "connection reset"
            raise CrashError(msg)
        if self.fail_next_send:
            self.fail_next_send = False
            msg = "connection reset"
            raise CrashError(msg)
        key = (self.device_id, delivery.transaction_id)
        event_id = self.events.get(key)
        if event_id is None:
            event_id = f"$sent{len(self.room_events)}"
            self.events[key] = event_id
            self.room_events.append((delivery.turn_id, event_id))
            self.accepted_stages.append(delivery.stage)
        if self.lose_acknowledgement:
            self.lose_acknowledgement = False
            msg = "crashed after Matrix accepted the message"
            raise CrashError(msg)
        return event_id

    async def find_delivered(self, delivery: OutboxDelivery) -> str | None:
        """Return the answer this turn already has in the room, by scanning for it.

        Stands in for the backward room-history scan the gateway runs, and
        counts itself so a test can assert that the ordinary path never pays
        for one.
        """
        self.room_scans += 1
        found = [event_id for turn_id, event_id in self.room_events if turn_id == delivery.turn_id]
        if len(found) > 1:
            msg = f"turn {delivery.turn_id!r} already has {len(found)} visible answers"
            raise AssertionError(msg)
        return next(iter(found), None)

    @property
    def visible_messages(self) -> int:
        """Return how many distinct events this server actually holds."""
        return len(self.room_events)


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
        """Return a fresh delivery view, as a restart would.

        The device is read from the homeserver each time rather than held, so
        a test that re-logs in gets a delivery bound to the new device exactly
        as a restarted process would.
        """
        return ResponseDelivery(
            store=self.store,
            send=self.homeserver.send,
            sending_device_id=self.homeserver.device_id,
            resolve_delivered=self.homeserver.find_delivered,
        )

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

    async def handle(self, event: JournalEvent) -> bool:
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
            sending_device_id=self.homeserver.device_id,
            resolve_delivered=self.homeserver.find_delivered,
            handoff=_SETTLE_THE_SOURCE,
        ).deliver(
            turn_id=event.event_id,
            stage=DeliveryStage.FINAL,
            room_id=event.room_id,
            thread_id=event.thread_id,
            payload={"msgtype": "m.text", "body": answer},
        )
        # The handoff already settled this source. Settling it here as well
        # would be a second authority over the same fact.
        return False

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


async def _forget_the_sending_device(runtime: TurnRuntime) -> None:
    """Blank the recorded device, as a database predating the column holds it.

    Written as a real UPDATE rather than a patched read, because the thing
    under test is how the delivery path reads a column that is NULL on disk.
    """
    await runtime.crashing_backend.inner.write(
        lambda transaction: transaction.execute(
            "UPDATE response_outbox SET sending_device_id = NULL WHERE principal_id = ?",
            (PRINCIPAL,),
        ),
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

        await runtime.store.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await runtime.delivery.recover()
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
        assert runtime.homeserver.visible_messages == 1
        assert runtime.model_runs == 1

    async def test_a_rejoin_after_a_send_that_never_arrived_delivers_the_old_answer(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The other rejoin branch, and the one that costs something.

        Its sibling above covers the send Matrix accepted. This is the send
        that never arrived: the row was claimed, the network failed, and the
        server holds nothing. The bot then leaves and rejoins, and recovery
        resends -- so an answer authored for the previous membership appears
        inside the new one, late.

        Nothing in the row can distinguish this case from the accepted one.
        Both are `attempted` with an unknown network outcome, and the two want
        opposite things: resending is the only convergent move if the send
        landed, and is a stale delivery if it did not. The fence is drawn by
        `attempted` and the choice is to resend, because answering a question
        that really was asked, slightly late, in a room the bot has rejoined,
        is a smaller harm than posting the answer twice or leaving the durable
        record and the room permanently disagreeing.

        So this asserts the cost rather than guarding against it. An
        unattempted row is the opposite case and is deleted by the fence: it
        was written for a conversation the bot has left and nothing outside
        this process has seen it.
        """
        await admit(runtime.store)
        runtime.homeserver.fail_next_send = True
        await runtime.worker().drain_once()
        # The claim committed and the send did not, so the server holds nothing.
        assert runtime.homeserver.visible_messages == 0

        await runtime.store.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await runtime.delivery.recover()

        # The deliberate cost: the previous membership's answer lands in the new one.
        assert runtime.homeserver.visible_messages == 1
        # Still exactly one, and still one model run -- late, not duplicated.
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

    async def test_a_refused_enqueue_hands_nothing_over(self, runtime: TurnRuntime) -> None:
        """A fenced answer leaves no row, and hands over nothing of its own.

        The enqueue's settlement rides inside its write, so a refused enqueue
        must not settle anything: there would be no row owing the answer it
        just discharged.

        The source is nonetheless terminal here, and not by this enqueue -- the
        fence retired it. That distinction is the whole point. An earlier
        version left it pending on the reasoning that work must keep an owner,
        but no owner exists: every future enqueue for a turn admitted under the
        old epoch is refused by the same fence, so the source could only be
        offered, re-run, and refused again on every restart.
        """
        await admit(runtime.store)
        await runtime.store.fence_departure(ROOM, source=DepartureSource.LOCAL)
        assert await runtime.store.pending() == (), "the fence left unanswerable work offered"

        transaction_id = await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
            settle_source_event_ids=(SOURCE,),
        )

        assert transaction_id is None
        assert await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL) is None
        settled = await runtime.store.load_event(SOURCE)
        assert settled is not None, "the journal row is still the proof this event had its turn"


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

        # Acknowledged deliveries leave the recovery set, and the never-sent
        # placeholder is withdrawn, so a second restart sends nothing.
        assert (await runtime.delivery.recover()).recovered == 0
        assert runtime.homeserver.sends == sends_after_recovery
        assert runtime.homeserver.visible_messages == 1
        assert await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.INITIAL) is None

    async def test_live_final_cannot_overtake_initial_recovery_before_matrix_acceptance(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A FINAL cannot become visible while recovery has an earlier INITIAL in flight.

        Recovery has already listed the unacknowledged placeholder and proved
        no FINAL exists when its Matrix request pauses immediately before
        acceptance. The live turn then tries to deliver FINAL under its
        distinct transaction ID. Letting that request finish first makes the
        later placeholder newly visible after the answer.
        """
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "thinking"},
        )
        initial_reached_matrix = asyncio.Event()
        accept_initial = asyncio.Event()

        async def paused_before_initial_acceptance(delivery: OutboxDelivery) -> str:
            if delivery.stage is DeliveryStage.INITIAL:
                initial_reached_matrix.set()
                await accept_initial.wait()
            return await runtime.homeserver.send(delivery)

        delivery_state = runtime.delivery
        recovery_delivery = replace(delivery_state, send=paused_before_initial_acceptance)
        live_delivery = replace(delivery_state, send=paused_before_initial_acceptance)
        recovery = asyncio.create_task(recovery_delivery.recover())
        await initial_reached_matrix.wait()
        final = asyncio.create_task(
            live_delivery.deliver(
                turn_id=SOURCE,
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "answer"},
            ),
        )
        completed_before_initial_acceptance, _ = await asyncio.wait({final}, timeout=0.2)

        accept_initial.set()
        await recovery
        final_event_id = await final

        assert final not in completed_before_initial_acceptance, (
            "FINAL became visible while recovery's earlier INITIAL was paused before Matrix acceptance"
        )
        assert final_event_id is not None
        assert runtime.homeserver.accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]

    async def test_failed_initial_without_a_final_remains_recoverable(self, runtime: TurnRuntime) -> None:
        """An INITIAL that Matrix never accepted remains owed until a later pass sends it."""
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "thinking"},
        )
        runtime.homeserver.fail_next_send = True

        failed = await runtime.delivery.recover()
        recovered = await runtime.delivery.recover()

        assert failed.failed == 1
        assert recovered.recovered == 1
        assert runtime.homeserver.accepted_stages == [DeliveryStage.INITIAL]

    async def test_final_retries_an_attempted_initial_before_it_becomes_visible(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A crashed sender's late INITIAL acceptance cannot land after FINAL.

        Repeating INITIAL under its frozen transaction first establishes the
        placeholder's event. The abandoned request then deduplicates onto that
        event even when it returns after FINAL, so it cannot append a late
        placeholder to the room.
        """
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "thinking"},
        )
        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.INITIAL)
        assert claimed is not None
        await runtime.store.record_sending_device(
            turn_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            device_id=runtime.homeserver.device_id,
        )
        remote_started = asyncio.Event()
        accept_initial = asyncio.Event()

        async def abandoned_matrix_request() -> str:
            remote_started.set()
            await accept_initial.wait()
            return await runtime.homeserver.send(claimed)

        remote_initial = asyncio.create_task(abandoned_matrix_request())
        await remote_started.wait()
        final_event_id = await runtime.delivery.deliver(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        accept_initial.set()
        await remote_initial

        recovered = await runtime.delivery.recover()

        assert final_event_id == "$sent1"
        assert recovered.complete
        assert runtime.homeserver.accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]
        assert runtime.homeserver.visible_messages == 2

    async def test_initial_cannot_become_visible_after_final_owns_delivery(self, runtime: TurnRuntime) -> None:
        """A stale INITIAL waiting behind an in-flight FINAL is suppressed after FINAL completes."""
        final_reached_matrix = asyncio.Event()
        accept_final = asyncio.Event()

        async def paused_before_final_acceptance(delivery: OutboxDelivery) -> str:
            if delivery.stage is DeliveryStage.FINAL:
                final_reached_matrix.set()
                await accept_final.wait()
            return await runtime.homeserver.send(delivery)

        delivery_state = runtime.delivery
        final_delivery = replace(delivery_state, send=paused_before_final_acceptance)
        initial_delivery = replace(delivery_state, send=paused_before_final_acceptance)
        final = asyncio.create_task(
            final_delivery.deliver(
                turn_id=SOURCE,
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "answer"},
            ),
        )
        await final_reached_matrix.wait()
        initial = asyncio.create_task(
            initial_delivery.deliver(
                turn_id=SOURCE,
                stage=DeliveryStage.INITIAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "thinking"},
            ),
        )
        completed_before_final_acceptance, _ = await asyncio.wait({initial}, timeout=0.2)

        accept_final.set()
        final_event_id = await final
        initial_event_id = await initial

        assert initial not in completed_before_final_acceptance
        assert final_event_id is not None
        assert initial_event_id is None
        assert runtime.homeserver.accepted_stages == [DeliveryStage.FINAL]


class TestARelogInCannotDuplicateTheAnswer:
    """The transaction ID's guarantee ends at the device that holds it.

    Everything else in this file rests on one sentence: a resend under the
    frozen transaction ID collapses onto the event the first attempt produced.
    That sentence has a scope, and it is the device. Between the attempt and
    the retry a process can restart into a fresh login -- cleared state, a
    rotated credential, a re-provisioned account -- and the ID it kept is then
    one the homeserver has never seen from the device now using it.
    """

    async def test_the_transaction_id_stops_deduplicating_across_a_relogin(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The premise, asserted against the fake rather than assumed of it.

        If this ever fails the rest of the class proves nothing, because the
        duplicate those tests prevent would not be reachable in the first
        place.
        """
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert claimed is not None

        first = await runtime.homeserver.send(claimed)
        same_device = await runtime.homeserver.send(claimed)
        assert same_device == first
        assert runtime.homeserver.visible_messages == 1

        runtime.homeserver.device_id = "DEVICE2"
        after_relogin = await runtime.homeserver.send(claimed)

        assert after_relogin != first
        assert runtime.homeserver.visible_messages == 2

    async def test_a_resend_after_a_relogin_adopts_the_answer_already_sent(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The bug this closes: the answer is in the room, and stays one answer.

        Matrix accepted the message and the process died before recording the
        event ID, so the row is unacknowledged and looks exactly like one that
        never arrived. Recovery then runs under a different device, where the
        frozen transaction ID buys nothing. Resending blind would post the
        answer a second time.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()

        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert stored.sending_device_id == "DEVICE1"
        assert runtime.homeserver.visible_messages == 1
        delivered_event_id = runtime.homeserver.room_events[0][1]

        runtime.homeserver.device_id = "DEVICE2"
        sends_before = runtime.homeserver.sends
        outcome = await runtime.delivery.recover()

        assert outcome.failed == 0
        assert runtime.homeserver.visible_messages == 1, "the answer was posted twice"
        assert runtime.homeserver.sends == sends_before, "recovery sent again instead of adopting"
        settled = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert settled is not None
        assert settled.acknowledged_event_id == delivered_event_id

    async def test_a_resend_after_a_relogin_still_sends_what_never_arrived(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The other half: refusing to resend would silently drop the answer.

        The device changed, but nothing was ever delivered -- the send failed
        before the homeserver saw it. A guard that treats "cannot prove it
        arrived" as "it arrived" would leave a user waiting forever, which is
        strictly worse than the duplicate it is avoiding.
        """
        await admit(runtime.store)
        runtime.homeserver.fail_next_send = True
        await runtime.worker().drain_once()

        assert runtime.homeserver.visible_messages == 0

        runtime.homeserver.device_id = "DEVICE2"
        outcome = await runtime.delivery.recover()

        assert outcome.recovered == 1
        assert outcome.failed == 0
        assert runtime.homeserver.visible_messages == 1
        assert runtime.homeserver.room_scans == 1, "the room is what decides, so it has to be read"

    async def test_the_ordinary_restart_never_scans_the_room(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A restart that keeps its device pays nothing for this guard.

        The overwhelmingly common recovery is the same process's own device
        coming back. Its transaction ID is still good, so the resend collapses
        on the homeserver and no history scan happens at all. A guard that
        scanned unconditionally would put a backward pagination in front of
        every recovered answer.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()

        outcome = await runtime.delivery.recover()

        assert outcome.failed == 0
        assert runtime.homeserver.room_scans == 0
        assert runtime.homeserver.visible_messages == 1

    async def test_an_attempted_row_with_no_recorded_device_is_reconciled(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Unknown is not "unchanged", and reading it that way duplicates.

        A row written before the column existed is attempted by a device
        nobody recorded. An earlier version of this guard called that safe and
        resent blind, which is only correct if the device happens not to have
        changed. Here it has: the answer is already in the room under the old
        device, the frozen transaction ID means nothing to the new one, and a
        blind resend posts it twice.

        The room is what settles it, exactly as for a known device change.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()

        await _forget_the_sending_device(runtime)
        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.attempted
        assert stored.sending_device_id is None
        assert runtime.homeserver.visible_messages == 1
        delivered_event_id = runtime.homeserver.room_events[0][1]

        runtime.homeserver.device_id = "DEVICE2"
        outcome = await runtime.delivery.recover()

        assert outcome.failed == 0
        assert runtime.homeserver.visible_messages == 1, "the answer was posted twice"
        assert runtime.homeserver.room_scans == 1, "an unprovable device has to read the room"
        settled = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert settled is not None
        assert settled.acknowledged_event_id == delivered_event_id

    async def test_a_delivery_nobody_has_attempted_never_scans(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """What bounds the cost of treating unknown as changed.

        An unattempted row has no recorded device for the uninteresting
        reason that nothing has sent it yet, so there is no earlier event for
        a resend to collide with. Folding that into "unknown" would put a
        backward pagination in front of every first delivery, which is the
        common case rather than the rare one.
        """
        await admit(runtime.store)

        await runtime.worker().drain_once()

        assert runtime.homeserver.room_scans == 0
        assert runtime.homeserver.visible_messages == 1

    async def test_a_process_that_cannot_name_its_own_device_reconciles(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The other unknown: this side of the comparison, not the row's.

        A delivery that cannot name the device it is about to send from cannot
        prove the row's device is the same one, so it is in exactly the
        position the row-side unknown is in. Reconciling costs a scan; the
        alternative asserts an identity nothing established.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()

        outcome = await replace(runtime.delivery, sending_device_id=None).recover()

        assert outcome.failed == 0
        assert runtime.homeserver.room_scans == 1
        assert runtime.homeserver.visible_messages == 1

    async def test_an_edit_is_resent_across_a_relogin_without_a_scan(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """An edit cannot duplicate a visible message, so it does not pay to check.

        A second `m.replace` carrying the same content resolves to the same
        message as the first. The stale transaction ID does admit a duplicate
        event, but not one anybody can see, and a room scan to avoid it would
        buy nothing.
        """
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
            edits_event_id="$placeholder",
        )
        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert claimed is not None
        await runtime.store.record_sending_device(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            device_id="DEVICE1",
        )

        runtime.homeserver.device_id = "DEVICE2"
        outcome = await runtime.delivery.recover()

        assert outcome.recovered == 1
        assert runtime.homeserver.room_scans == 0

    async def test_the_device_is_recorded_before_the_send_but_not_at_the_claim(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Two different moments, and the gap between them is load-bearing.

        The marker has to be committed before the network call, for the reason
        `attempted` is: a crash during the send must leave behind the fact that
        this device may already hold the transaction ID.

        It must not be committed at the claim. Claiming does not mean this
        device is going to send -- when the recorded device differs, delivery
        reads the room first, and that read can fail. Stamping the marker
        before knowing erases the evidence that the read is still owed, and the
        next pass then sees its own device and sends blind.
        """
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        before = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert before is not None
        assert not before.attempted
        assert before.sending_device_id is None

        claimed = await runtime.store.claim_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert claimed is not None
        assert not claimed.attempted, "a claim reports the state it took over, not the one it wrote"
        assert claimed.sending_device_id is None

        after_claim = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert after_claim is not None
        assert after_claim.attempted, "the claim froze the payload"
        assert after_claim.sending_device_id is None, "the claim stamped a device it had not committed to"

        await runtime.store.record_sending_device(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            device_id="DEVICE1",
        )
        before_send = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert before_send is not None
        assert before_send.sending_device_id == "DEVICE1"

    async def test_a_room_scan_that_raises_leaves_the_lookup_still_owed(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The marker must not move for a send that never happened.

        Matrix accepted D1's answer and the acknowledgement was lost, so the
        row looks exactly like one that never arrived. Recovery under D2 has to
        read the room, and here the read fails -- the homeserver is briefly
        unreachable.

        If the claim had already stamped the row D2, the next pass would
        compare D2 against D2, conclude the transaction ID still protects it,
        and send. The user would see the answer twice. Leaving the marker where
        it was keeps the lookup owed, and the cost is that the next pass scans
        again.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()
        assert runtime.homeserver.visible_messages == 1

        scans = 0

        async def unreachable(_delivery: OutboxDelivery) -> str | None:
            nonlocal scans
            scans += 1
            msg = "the homeserver could not be reached"
            raise CrashError(msg)

        runtime.homeserver.device_id = "DEVICE2"
        recovery = replace(runtime.delivery, resolve_delivered=unreachable)

        assert (await recovery.recover()).failed == 1
        stranded = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stranded is not None
        assert stranded.sending_device_id == "DEVICE1", "a failed lookup took ownership of the row"

        assert (await recovery.recover()).failed == 1
        assert scans == 2, "the second pass skipped the lookup it still owed"
        assert runtime.homeserver.visible_messages == 1, "the answer was posted twice"

    async def test_a_failing_resend_scans_the_room_once_per_relogin(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The scan is bounded by device changes, not by recovery passes.

        Recovery runs after every sync response, so anything it does per
        unacknowledged row it does forever until that row resolves. A backward
        room pagination on that schedule would be a real cost, and a send that
        keeps failing keeps the row in the set.

        What bounds it is where the device is written: the claim records the
        device about to send, not the one that succeeded. The first pass after
        a re-login sees a mismatch and scans; it also leaves the row naming the
        current device, so every pass after that is an ordinary resend even
        though the send is still failing.
        """
        await admit(runtime.store)
        runtime.homeserver.fail_next_send = True
        await runtime.worker().drain_once()

        runtime.homeserver.device_id = "DEVICE2"
        runtime.homeserver.fail_sends_until = runtime.homeserver.sends + 3

        for _ in range(3):
            assert (await runtime.delivery.recover()).failed == 1

        assert runtime.homeserver.room_scans == 1, "the room was re-paginated on a later pass"
        assert runtime.homeserver.visible_messages == 0

        assert (await runtime.delivery.recover()).recovered == 1
        assert runtime.homeserver.visible_messages == 1
        assert runtime.homeserver.room_scans == 1


class TestOneTurnStageIsOneMessage:
    """What a second delivery under the same (turn, stage) actually does.

    Every caller routed through the outbox has to send at most once per turn
    and stage, and the reason is easy to state wrongly. It is not that the
    second attempt is rejected -- an error a caller could notice. The enqueue
    returns a transaction ID, the claim returns the first attempt's event, and
    the second text is silently discarded, so the caller is told it delivered
    something it did not.

    That is the whole constraint behind splitting a placeholder and its answer
    across the two stages instead of sending both as answers, and it is worth a
    test of its own because every future caller depends on it being true.
    """

    async def test_a_second_answer_for_one_turn_keeps_the_first_text(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The freeze rule wins, and reports success while it does."""
        await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
        )
        first = await runtime.delivery.flush(turn_id=SOURCE, stage=DeliveryStage.FINAL)

        transaction_id = await runtime.store.enqueue_delivery(
            turn_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "a different answer"},
        )
        second = await runtime.delivery.flush(turn_id=SOURCE, stage=DeliveryStage.FINAL)

        assert transaction_id is not None, "the enqueue reports success rather than refusing"
        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "the answer", "the second text overwrote a frozen row"
        assert second == first, "the caller was handed a new event for text that never went out"
        assert runtime.homeserver.visible_messages == 1

    async def test_the_two_stages_do_not_collide(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A placeholder and its answer are different rows, so both are sent.

        The escape from the rule above, and the reason a caller that needs two
        visible messages for one turn stages them rather than sending two
        answers.
        """
        for stage, body in ((DeliveryStage.INITIAL, "thinking"), (DeliveryStage.FINAL, "the answer")):
            await runtime.store.enqueue_delivery(
                turn_id=SOURCE,
                stage=stage,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": body},
            )
            await runtime.delivery.flush(turn_id=SOURCE, stage=stage)

        assert runtime.homeserver.visible_messages == 2
        placeholder = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.INITIAL)
        answer = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert placeholder is not None
        assert answer is not None
        assert placeholder.payload["body"] == "thinking"
        assert answer.payload["body"] == "the answer"
        assert placeholder.transaction_id != answer.transaction_id
