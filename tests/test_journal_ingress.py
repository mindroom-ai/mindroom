"""Provenance mapping, durable admission, and pending-event execution."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.event_journal import EventClass, EventKind, SettlementOutcome
from mindroom.matrix.journal_ingress import (
    JournalCorruptionError,
    JournalIngress,
    _event_class_for,
    _event_kind,
    inbound_event,
    parse_journal_event,
    projected_event,
)
from mindroom.pending_event_worker import _BATCH_SIZE, PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sized

    from mindroom.event_journal import EventJournalStore, JournalEvent, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def text_event(
    event_id: str,
    body: str = "hello",
    *,
    thread_id: str | None = None,
    ts: int = 1_000,
) -> nio.Event:
    """Return a parsed text message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "type": "m.room.message",
            "content": content,
        },
    )
    assert isinstance(event, nio.Event)
    return event


def redaction_event(event_id: str, redacts: str, *, ts: int = 2_000) -> nio.Event:
    """Return a parsed redaction event."""
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "type": "m.room.redaction",
            "redacts": redacts,
            "content": {},
        },
    )
    assert isinstance(event, nio.Event)
    return event


def room() -> nio.MatrixRoom:
    """Return a minimal joined room."""
    return nio.MatrixRoom(ROOM, ALICE)


class TestProvenanceMapping:
    """nio owns provenance; MindRoom owns only what it means."""

    @pytest.mark.parametrize(
        ("provenance", "expected"),
        [
            (nio.TimelineEventProvenance.LIVE, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.RECOVERED, EventClass.ACTIONABLE),
            (nio.TimelineEventProvenance.HISTORY, EventClass.CONTEXT_ONLY),
        ],
    )
    async def test_provenance_decides_whether_work_may_start(
        self,
        provenance: nio.TimelineEventProvenance,
        expected: EventClass,
    ) -> None:
        """Provenance decides whether work may start."""
        assert _event_class_for(provenance) is expected

    async def test_every_provenance_is_mapped(self) -> None:
        """A new provenance must not silently default to actionable."""
        for provenance in nio.TimelineEventProvenance:
            assert _event_class_for(provenance) in EventClass


class TestEventKinds:
    """One event carries at most one semantic purpose."""

    async def test_a_text_message_is_a_message(self) -> None:
        """A text message is a message."""
        assert _event_kind(text_event("$m")) is EventKind.MESSAGE

    async def test_a_redaction_is_a_redaction(self) -> None:
        """A redaction is a redaction."""
        assert _event_kind(redaction_event("$r", "$m")) is EventKind.REDACTION

    async def test_an_unrelated_event_has_no_kind(self) -> None:
        """An unrelated event has no kind."""
        event = nio.Event.parse_event(
            {
                "event_id": "$topic",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.topic",
                "state_key": "",
                "content": {"topic": "hi"},
            },
        )
        assert isinstance(event, nio.Event)
        assert _event_kind(event) is None


class TestAdmissionAdapter:
    """The translation from a nio event to a durable row."""

    async def test_a_threaded_message_lands_in_its_thread(self) -> None:
        """A threaded message lands in its thread."""
        inbound = inbound_event(
            ROOM,
            text_event("$m", thread_id="$root"),
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
        )
        assert inbound.thread_id == "$root"

    async def test_an_unthreaded_message_has_no_thread(self) -> None:
        """An unthreaded message has no thread."""
        inbound = inbound_event(ROOM, text_event("$m"), EventKind.MESSAGE, EventClass.ACTIONABLE)
        assert inbound.thread_id is None

    async def test_a_reaction_does_not_touch_the_projection(self) -> None:
        """A reaction does not touch the projection."""
        event = nio.Event.parse_event(
            {
                "event_id": "$reaction",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.reaction",
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$m", "key": "x"}},
            },
        )
        assert isinstance(event, nio.Event)
        assert projected_event(ROOM, event, EventKind.REACTION) is None

    async def test_a_redaction_projects_onto_its_target(self) -> None:
        """A redaction projects onto its target."""
        projected = projected_event(ROOM, redaction_event("$r", "$m"), EventKind.REDACTION)
        assert projected is not None
        assert projected.redacts_event_id == "$m"

    async def test_a_redaction_without_a_target_never_reaches_the_journal(self) -> None:
        """Nio's schema requires the target, so such an event is never parsed.

        Worth pinning: the projection reads the typed ``redacts`` attribute
        rather than probing the source, and that is only safe while nio refuses
        to produce a redaction with no target.
        """
        event = nio.Event.parse_event(
            {
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.redaction",
                "content": {},
            },
        )
        assert not isinstance(event, nio.RedactionEvent)
        assert _event_kind(event) is not EventKind.REDACTION


class TestReplayFidelity:
    """A recovered event must be the same event that was admitted."""

    async def test_a_message_replays_as_itself(self, alice: PrincipalStore) -> None:
        """A message replays as itself."""
        original = text_event("$m", "hello")
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MESSAGE),
        )

        stored = (await alice.pending())[0]
        replayed = parse_journal_event(stored)

        assert isinstance(replayed, nio.RoomMessageText)
        assert replayed.event_id == "$m"
        assert replayed.body == "hello"

    async def test_decryption_results_survive_replay(self, alice: PrincipalStore) -> None:
        """Nio attaches these after parsing, so they are not in the source.

        Losing them would replay a decrypted event as an untrusted one, which
        changes what the authorization layer is allowed to do with it.
        """
        original = text_event("$m", "secret")
        original.decrypted = True
        original.verified = True
        original.sender_key = "key"
        original.session_id = "session"

        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, original, EventKind.MESSAGE),
        )
        replayed = parse_journal_event((await alice.pending())[0])

        assert replayed.decrypted
        assert replayed.verified
        assert replayed.sender_key == "key"
        assert replayed.session_id == "session"

    async def test_a_corrupt_payload_is_refused_not_guessed(self, alice: PrincipalStore) -> None:
        """A corrupt payload is refused not guessed."""
        original = text_event("$m")
        await alice.admit(
            inbound_event(ROOM, original, EventKind.MESSAGE, EventClass.ACTIONABLE),
            None,
        )
        stored = (await alice.pending())[0]
        corrupted = replace(stored, source={**stored.source, "event_id": "$different"})

        with pytest.raises(JournalCorruptionError):
            parse_journal_event(corrupted)


class TestDurableAdmission:
    """nio hears "accepted" only after the transaction commits."""

    async def test_an_admitted_event_becomes_pending_work(self, alice: PrincipalStore) -> None:
        """An admitted event becomes pending work."""
        ingress = JournalIngress(store=alice)

        await ingress._admit(room(), text_event("$m"), nio.TimelineEventProvenance.LIVE)

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_cold_history_populates_context_without_work(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Cold history populates context without work."""
        ingress = JournalIngress(store=alice)

        await ingress._admit(room(), text_event("$m", "old"), nio.TimelineEventProvenance.HISTORY)

        assert await alice.pending() == ()
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [m.content["body"] for m in page.messages] == ["old"]

    async def test_a_failed_admission_refuses_the_callback(self) -> None:
        """Refusing is what keeps the event for redelivery instead of losing it."""

        class Failing:
            principal_id = "agent@alice"

            async def admit(self, *_args: object, **_kwargs: object) -> None:
                msg = "disk is full"
                raise RuntimeError(msg)

        ingress = JournalIngress(store=Failing())  # type: ignore[arg-type]

        with pytest.raises(nio.CallbackNotAcceptedError):
            await ingress._admit(room(), text_event("$m"), nio.TimelineEventProvenance.LIVE)

    async def test_redelivery_after_a_crash_creates_one_turn(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nio redelivers what it was never told was accepted."""
        ingress = JournalIngress(store=alice)
        event = text_event("$m")

        await ingress._admit(room(), event, nio.TimelineEventProvenance.LIVE)
        await ingress._admit(room(), event, nio.TimelineEventProvenance.RECOVERED)

        assert [journal.event_id for journal in await alice.pending()] == ["$m"]

    async def test_an_unowned_event_is_neither_admitted_nor_rejected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unowned event is neither admitted nor rejected."""
        ingress = JournalIngress(store=alice)
        topic = nio.Event.parse_event(
            {
                "event_id": "$topic",
                "sender": ALICE,
                "origin_server_ts": 1,
                "type": "m.room.topic",
                "state_key": "",
                "content": {"topic": "hi"},
            },
        )
        assert isinstance(topic, nio.Event)

        await ingress._admit(room(), topic, nio.TimelineEventProvenance.LIVE)

        assert await alice.pending() == ()


class TestPendingEventWorker:
    """Execution order, failure isolation, and crash behavior."""

    @staticmethod
    async def _admit(store: PrincipalStore, event: nio.Event, room_id: str = ROOM) -> None:
        await store.admit(
            inbound_event(room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(room_id, event, EventKind.MESSAGE),
        )

    async def test_a_rooms_events_run_in_receipt_order(self, alice: PrincipalStore) -> None:
        """A rooms events run in receipt order."""
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$second", ts=9_000))
        await self._admit(alice, text_event("$first", ts=1_000))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert handled == ["$second", "$first"]

    async def test_a_settled_event_never_runs_again(self, alice: PrincipalStore) -> None:
        """A settled event never runs again."""
        runs = 0

        async def handle(event: JournalEvent) -> SettlementOutcome:
            nonlocal runs
            runs += 1
            del event
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)

        await worker.drain_once()
        await worker.drain_once()

        assert runs == 1

    async def test_a_failed_event_stays_pending(self, alice: PrincipalStore) -> None:
        """A failed event stays pending."""

        async def handle(event: JournalEvent) -> SettlementOutcome:
            del event
            msg = "model unavailable"
            raise RuntimeError(msg)

        await self._admit(alice, text_event("$m"))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_a_failure_stops_that_rooms_later_events(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the room is answered out of order, and the retry lands last."""
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            if event.event_id == "$first":
                msg = "model unavailable"
                raise RuntimeError(msg)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$first", ts=1_000))
        await self._admit(alice, text_event("$second", ts=2_000))

        await PendingEventWorker(store=alice, handle=handle).drain_once()

        assert handled == ["$first"]
        assert {event.event_id for event in await alice.pending()} == {"$first", "$second"}

    async def test_one_stalled_room_does_not_block_another(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One stalled room does not block another."""
        other_room = "!other:example.org"
        released = asyncio.Event()
        fast_finished = asyncio.Event()
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            if event.room_id == ROOM:
                await released.wait()
            handled.append(event.event_id)
            if event.room_id == other_room:
                fast_finished.set()
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$slow"))
        await self._admit(alice, text_event("$fast"), room_id=other_room)

        worker = PendingEventWorker(store=alice, handle=handle)
        draining = asyncio.create_task(worker.drain_once())

        # The other room finishes while this one is still blocked, which is
        # only possible if the lanes are genuinely independent.
        await asyncio.wait_for(fast_finished.wait(), timeout=5)
        assert handled == ["$fast"]

        released.set()
        await draining
        assert handled == ["$fast", "$slow"]

    async def test_cancellation_leaves_the_event_pending(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A crash mid-turn must make the event eligible again, not stranded."""
        started = asyncio.Event()

        async def handle(event: JournalEvent) -> SettlementOutcome:
            del event
            started.set()
            await asyncio.sleep(3600)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await asyncio.wait_for(started.wait(), timeout=5)

        await worker.stop()

        assert [event.event_id for event in await alice.pending()] == ["$m"]

    async def test_a_restart_resumes_what_the_previous_process_left(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A restart resumes what the previous process left."""
        handled: list[str] = []

        async def never(event: JournalEvent) -> SettlementOutcome:
            del event
            await asyncio.sleep(3600)
            return SettlementOutcome.SUCCEEDED

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        crashed = PendingEventWorker(store=alice, handle=never)
        crashed.start()
        await asyncio.sleep(0.05)
        await crashed.stop()

        restarted = PendingEventWorker(store=alice, handle=handle)
        await restarted.drain_once()

        assert handled == ["$m"]

    async def test_a_backlog_larger_than_one_batch_is_fully_drained(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A bound that drops the remainder abandons durable work silently.

        Driven through the pump rather than a drain, because only the pump has
        to arrange its own next look: nothing admits a further event afterwards
        to wake it, so a scan that stops at one page strands the rest forever.
        """
        count = _BATCH_SIZE + 1
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        for index in range(count):
            await self._admit(alice, text_event(f"$m{index:04d}", ts=1_000 + index))

        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await _eventually_async(lambda: alice.pending())
        await worker.stop()

        assert len(handled) == count

    async def test_work_admitted_while_a_lane_runs_is_still_dispatched(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The lost wakeup that leaves a live room permanently unanswered.

        The pump is woken while the room's lane is busy, so it cannot start a
        second one. Unless the finishing lane arranges another look, the event
        admitted during that window stays pending forever even though the
        process is healthy and still syncing.
        """
        released = asyncio.Event()
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            if event.event_id == "$slow":
                await released.wait()
            handled.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        worker = PendingEventWorker(store=alice, handle=handle)
        await self._admit(alice, text_event("$slow", ts=1_000))
        worker.start()
        await _eventually(lambda: worker._lanes != {})

        await self._admit(alice, text_event("$late", ts=2_000))
        worker.wake()
        await asyncio.sleep(0)
        released.set()

        await _eventually(lambda: handled == ["$slow", "$late"])
        await worker.stop()

    async def test_a_deferred_turn_does_not_hide_the_events_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A turn still running stays pending, so a scan must look past it.

        A full page of in-flight turns is exactly what a busy bot looks like.
        If the scan stops at the first one it cannot act on, every event queued
        behind them is invisible until those turns happen to finish.
        """
        handled: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome | None:
            handled.append(event.event_id)
            return None if event.event_id.startswith("$busy") else SettlementOutcome.SUCCEEDED

        for index in range(_BATCH_SIZE):
            await self._admit(alice, text_event(f"$busy{index:04d}", ts=1_000 + index), room_id=f"!r{index}:x")
        worker = PendingEventWorker(store=alice, handle=handle)
        worker.start()
        await _eventually(lambda: len(handled) == _BATCH_SIZE)
        handled.clear()

        await self._admit(alice, text_event("$behind", ts=9_000), room_id="!behind:x")
        worker.wake()

        await _eventually(lambda: handled == ["$behind"])
        await worker.stop()

    async def test_a_failed_lane_is_retried_without_another_admission(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Nothing else wakes the pump, so the failure has to schedule its own retry."""
        attempts: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome:
            attempts.append(event.event_id)
            if len(attempts) == 1:
                msg = "model unavailable"
                raise RuntimeError(msg)
            return SettlementOutcome.SUCCEEDED

        await self._admit(alice, text_event("$m"))
        worker = PendingEventWorker(store=alice, handle=handle)
        worker._retry_delay_seconds = 0.01
        worker.start()

        await _eventually(lambda: len(attempts) >= 2, seconds=10)
        await worker.stop()

        assert attempts == ["$m", "$m"]


async def _eventually_async(query: Callable[[], Awaitable[Sized]], *, seconds: float = 10.0) -> None:
    """Wait until a durable query comes back empty."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if not len(await query()):
            return
        await asyncio.sleep(0.01)
    msg = "The durable queue never drained"
    raise AssertionError(msg)


async def _eventually(predicate: Callable[[], bool], *, seconds: float = 5.0) -> None:
    """Wait for a background pump to reach a state, without fixed sleeps."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "The worker never reached the expected state"
    raise AssertionError(msg)
