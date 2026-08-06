"""Crash the turn pipeline at each of its nine boundaries.

A durable design is only as good as its worst interruption point. Each test
below stops the process at one specific moment, restarts everything that is
not durable, and then checks the two properties that matter: exactly one
terminal turn, and at most one visible response.

The model is counted as well. Enqueueing is what makes an answer durable, so a
crash before it costs a model run and a crash after it must not: the stored
payload is the answer, and asking the model for another one would only produce
a result that can never become visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventKind,
    SettlementOutcome,
)
from mindroom.event_journal.store import _DEFAULT_UNACKNOWLEDGED_LIMIT as _UNACKNOWLEDGED_BATCH
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.pending_event_worker import PendingEventWorker
from mindroom.response_delivery import ResponseDelivery

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore, JournalEvent, OutboxDelivery, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOT = "@mindroom_general:example.org"
SOURCE = "$inbound"


class CrashError(RuntimeError):
    """The process died here."""


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


@dataclass
class TurnRuntime:
    """Everything that would be rebuilt by a restart."""

    store: PrincipalStore
    homeserver: FakeHomeserver
    model_runs: int = 0
    crash_after_model: bool = False
    crash_after_enqueue: bool = False
    crash_before_settle: bool = False

    @property
    def delivery(self) -> ResponseDelivery:
        """Return a fresh delivery view, as a restart would."""
        return ResponseDelivery(store=self.store, send=self.homeserver.send)

    async def handle(self, event: JournalEvent) -> SettlementOutcome:
        """Run one turn: model, durable result, enqueue, claim, send, settle.

        A turn whose answer is already durable resumes from it. Spending the
        model again could only produce an answer the outbox would discard, so
        a restart picks up where the durable result left off.
        """
        durable = await self.store.load_delivery(turn_id=event.event_id, stage=DeliveryStage.FINAL)
        if durable is None:
            self.model_runs += 1
            answer = f"answer to {event.event_id}"
            if self.crash_after_model:
                msg = "crashed after the model finished"
                raise CrashError(msg)

            await self.store.enqueue_delivery(
                turn_id=event.event_id,
                stage=DeliveryStage.FINAL,
                room_id=event.room_id,
                thread_id=event.thread_id,
                payload={"msgtype": "m.text", "body": answer},
            )
        if self.crash_after_enqueue:
            msg = "crashed after enqueue, before claim"
            raise CrashError(msg)

        await self.delivery.flush(turn_id=event.event_id, stage=DeliveryStage.FINAL)
        if self.crash_before_settle:
            msg = "crashed after acknowledgement, before settlement"
            raise CrashError(msg)
        return SettlementOutcome.SUCCEEDED

    def worker(self) -> PendingEventWorker:
        """Return a fresh worker, as a restart would."""
        return PendingEventWorker(store=self.store, handle=self.handle)


@pytest.fixture
def runtime(journal_store: EventJournalStore) -> TurnRuntime:
    """Return one turn runtime over a real store."""
    return TurnRuntime(store=journal_store.principal("agent@alice"), homeserver=FakeHomeserver())


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

    async def test_six_after_enqueue_before_the_claim_commits(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """Six after enqueue before the claim commits."""
        await admit(runtime.store)
        runtime.crash_after_enqueue = True
        await runtime.worker().drain_once()

        stored = await runtime.store.load_delivery(turn_id=SOURCE, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert runtime.homeserver.sends == 0

        runtime.crash_after_enqueue = False
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

    async def test_nine_after_acknowledgement_before_settlement(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """The retry must not produce a second message."""
        await admit(runtime.store)
        runtime.crash_before_settle = True
        await runtime.worker().drain_once()

        assert await runtime.store.pending() != ()
        assert runtime.homeserver.visible_messages == 1

        runtime.crash_before_settle = False
        await runtime.worker().drain_once()

        await assert_settled_once(runtime)
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
        turn is still pending, so it runs again — and the room must still hold
        exactly one answer at the end of it.
        """
        await admit(runtime.store)
        runtime.homeserver.lose_acknowledgement = True
        await runtime.worker().drain_once()
        assert runtime.homeserver.visible_messages == 1

        await runtime.store.advance_membership_epoch(ROOM)
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

    async def test_a_send_failure_leaves_the_turn_retryable(
        self,
        runtime: TurnRuntime,
    ) -> None:
        """A send failure leaves the turn retryable."""
        await admit(runtime.store)
        runtime.homeserver.fail_next_send = True

        await runtime.worker().drain_once()
        assert await runtime.store.pending() != ()
        assert runtime.homeserver.visible_messages == 0

        await runtime.worker().drain_once()

        await assert_settled_once(runtime)


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
