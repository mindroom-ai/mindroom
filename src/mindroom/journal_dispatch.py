"""Turning admitted journal events into the typed Matrix callbacks MindRoom has.

The journal owns what was accepted and what still owes work. This owns the
fan-out: which callback runs for an event, whether the callback finished the
work or handed it to a turn, and who is allowed to consume a reaction that
several features could each claim.

The important asymmetry is between callbacks that finish and callbacks that
defer. A reaction is done when its handler returns. A message is not: it enters
coalescing and a turn, which may still be running long after the callback
returns. So a deferring handler leaves its event pending, and the source is
settled when its answer is durably owed to a room -- the FINAL outbox enqueue
-- or when the turn deliberately owes no answer at all. That is why a crash
mid-turn replays the message rather than losing the answer, and why a crash
after the answer is durable does not spend the model again.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.event_journal import (
    TURN_BACKED_KINDS,
    AdmissionResult,
    EventKind,
    SemanticConsumer,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.journal_ingress import (
    TEXTUAL_MESSAGE_EVENT_TYPE,
    JournalCorruptionError,
    JournalIngress,
    inbound_event,
    parse_journal_event,
    projected_event,
)
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, MatrixMediaEvent
from mindroom.pending_event_worker import PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import DispatchView, EventClass
    from mindroom.matrix.journal_ingress import TimelineMemberProvenance

from mindroom.event_journal import JournalEvent

logger = get_logger(__name__)

# The journal event whose callback is executing on this task. Callbacks reach
# it to claim a consumer or read their receipt order without every one of them
# having to thread the event through its own signature.
_RUNNING_EVENT: ContextVar[JournalEvent | None] = ContextVar("running_journal_event", default=None)

# How many unsettled lifecycle events one read of the identity walk covers. The
# walk continues past it; this only bounds how much is held at once.
_LIFECYCLE_PAGE_SIZE = 256

type _MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageFormatted], Awaitable[TurnDispatchOutcome]]
type _MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[TurnDispatchOutcome]]
type _ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
type _ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
type _RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
type _RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
type _DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JournalCallbacks:
    """The typed Matrix callbacks the journal dispatches to."""

    on_message: _MessageCallback
    on_media: _MediaCallback
    on_reaction: _ReactionCallback
    on_approval: _ApprovalCallback
    on_room_lifecycle: _RoomLifecycleCallback
    on_redaction: _RedactionCallback
    on_decryption_failure: _DecryptionFailureCallback
    source_has_live_owner: Callable[[str], bool]
    turn_has_live_claim: Callable[[str], bool]


@dataclass
class JournalDispatcher:
    """Admit Matrix events durably, then run their callbacks from the journal."""

    store: DispatchView
    # This bot's raw Matrix user ID, threaded through to admission so its own
    # streaming progress edits are recognized as transport and left out of the
    # conversation projection.
    self_sender: str
    callbacks: JournalCallbacks
    room_for_id: Callable[[str], nio.MatrixRoom]
    on_persist_failure: Callable[[], None] | None = None
    room_lifecycle_admission_enabled: Callable[[], bool] = lambda: False
    # Replaying a turn needs the agent fleet up, so the orchestrator releases
    # turn-backed replay separately from the rest of startup. Until it does,
    # those events stay pending; everything else drains immediately.
    _turn_replay_released: bool = field(default=False, init=False, repr=False)
    _worker: PendingEventWorker = field(init=False, repr=False)
    _ingress: JournalIngress = field(init=False, repr=False)
    # The event objects nio already parsed, kept until their callback runs.
    # Replaying from the stored payload is what recovery is for; doing it for
    # an event that is still in hand would parse every event twice and discard
    # the decryption state nio attached to the original.
    _live_events: dict[str, tuple[nio.MatrixRoom, nio.Event]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the worker and admission adapter this dispatcher owns."""
        self._worker = PendingEventWorker(
            store=self.store,
            handle=self._run_event,
            deferral_is_live=self._deferral_is_live,
            retained_event_ids=self._retained_live_event_ids,
            release_retained=self._forget_live_events,
        )
        self._ingress = JournalIngress(
            store=self.store,
            self_sender=self.self_sender,
            on_admitted=self._worker.wake,
            room_lifecycle_enabled=self.room_lifecycle_admission_enabled,
            on_event_admitted=self._remember_live_event,
            on_persist_failure=self.on_persist_failure or (lambda: None),
        )

    def _remember_live_event(self, room: nio.MatrixRoom, event: nio.Event) -> None:
        """Keep the room and event nio already produced, for their callback."""
        self._live_events[event.event_id] = (room, event)

    def _retained_live_event_ids(self) -> frozenset[str]:
        """Return the events whose parsed objects are still waiting for a run."""
        return frozenset(self._live_events)

    def _forget_live_events(self, event_ids: frozenset[str]) -> None:
        """Drop parsed objects for rows the worker proved no run can reach.

        Taking the object out is the last thing a run does, so a row that
        settles without one leaves it here forever. A membership fence is
        exactly that: it settles the turn-backed work the departure has made
        unanswerable, and the worker never sees the row again. What is left
        behind is a room, an event, and the message text inside it.
        """
        for event_id in event_ids:
            self._live_events.pop(event_id, None)

    def register(self, client: nio.AsyncClient) -> None:
        """Install durable admission ahead of every other callback."""
        self._ingress.register(client)

    def start(self) -> None:
        """Begin draining everything that does not need the agent fleet."""
        self._worker.start()

    def release_turn_replay(self) -> None:
        """Allow turn-backed events left by a previous process to replay."""
        self._turn_replay_released = True
        self._worker.wake()

    def wake(self) -> None:
        """Signal that newly admitted work is waiting."""
        self._worker.wake()

    @property
    def timeline_member_provenance(self) -> TimelineMemberProvenance:
        """Return what nio said about this response's room-member events."""
        return self._ingress.timeline_member_provenance

    def timeline_member_event_class(self, event: nio.Event) -> EventClass | None:
        """Return the class nio's provenance gives one member event, if it said."""
        return self._ingress.timeline_member_event_class(event)

    async def stop(self) -> None:
        """Stop draining, leaving unfinished work pending for the next start."""
        await self._worker.stop()

    async def drain_once(self) -> int:
        """Run everything currently pending to completion.

        This is the explicit recovery entry point, so it releases turn replay.
        What it deliberately does not do is forget which sources are in flight.
        A drain runs beside live turns rather than before them -- every bot
        that reports ready schedules one, and so does every hot reload -- and a
        source it hands to a second turn is not merely wasteful. `TurnStore`
        refuses the second claim, but refusing is not returning: the loser
        waits for the winner to settle, and it waits inside the room's lane, so
        the room answers nothing until the original turn ends.

        A deferral nobody kept is still reconsidered, because the liveness
        probe answers that question exactly rather than by assumption.
        """
        self._turn_replay_released = True
        return await self._worker.drain_once()

    async def admit_out_of_band(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        kind: EventKind,
        event_class: EventClass,
        *,
        live: bool = True,
    ) -> None:
        """Admit an event that does not arrive through timeline admission.

        Room-membership events are only owned once the router is ready for
        them, which is a decision the timeline callback cannot make.

        ``live=False`` admits the event without handing its parsed object to
        the callback, so the worker treats it as a replay. That is what a
        caller wants when it is recording work for a later process to run
        rather than delivering something that just happened.
        """
        try:
            admission = await self.store.admit(
                inbound_event(room.room_id, event, kind, event_class),
                projected_event(room.room_id, event, kind, self_sender=self.self_sender),
            )
        except Exception:
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        if live and admission is AdmissionResult.ADMITTED:
            # Only an event this admission created can still owe the run that
            # takes the parsed object back out again.
            self._remember_live_event(room, event)
        self._worker.wake()

    async def admit_and_run(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        kind: EventKind,
        event_class: EventClass,
    ) -> None:
        """Admit one out-of-band event and run its callback before returning.

        Membership hooks are ordered against the sync response that produced
        them, so their callback has to finish inside that response rather than
        whenever the worker next looks.

        Running it here does not exempt it from having one handler. Admission
        wakes the pump, two awaits separate that wake from the callback, and
        an event stays pending for as long as its handler runs -- so a scan in
        that window collects the event this is already running and dispatches
        it into the room's lane. Claiming it as this caller's sole handler is
        what closes that, and the claim is taken before admission because a
        scan can only collect a row that has committed.

        Draining through the room's lane, the way recovery does, is the wrong
        answer for this one. This runs on the sync task, and a lane can hold a
        message handler blocked on another turn settling -- which in turn can
        be waiting days for a tool-approval decision that only a live sync can
        deliver. Waiting for the lane here would trade a duplicate dispatch
        for a bot that receives nothing at all.
        """
        with self._worker.sole_handler(event.event_id):
            await self.admit_out_of_band(room, event, kind, event_class)
            stored = await self.store.load_event(event.event_id)
            if stored is None or not await self.store.is_pending(event.event_id):
                # A context-only event is admitted already settled, so no
                # callback will ever run for it. Keeping the parsed object
                # would hold it for a run that cannot come.
                self._live_events.pop(event.event_id, None)
                return
            if await self._run_event(stored):
                await self.store.settle(event.event_id)

    def _has_live_owner(self, event_id: str) -> bool:
        """Return whether something in this process is already holding one source.

        The single question both ends of a deferral ask. At dispatch it decides
        whether handing the source to a turn would put a second one inside it;
        on a later scan it decides whether the turn it was handed to still
        exists to hand it back. Two answers to that from two places is how a
        recovery pass ends up re-entering work it can see is running.
        """
        return self.callbacks.source_has_live_owner(event_id) or self.callbacks.turn_has_live_claim(event_id)

    def _deferral_is_live(self, event: JournalEvent) -> bool:
        """Return whether the owner one deferred event was handed to still exists.

        Mirrors the reasons ``_run_event`` defers, in the same order, because
        this is that question inverted: the event is still owed to someone only
        while the thing it was handed to is still there to hand it back.

        Every answer is conservative. A wrong "live" only reproduces the stall
        this replaces; a wrong "gone" costs a re-dispatch that ``TurnStore``
        then has to refuse.
        """
        if event.kind not in TURN_BACKED_KINDS:
            # A completing callback settles or raises. It never defers, so a
            # deferral for one of these kinds cannot exist to begin with.
            return True
        if not self._turn_replay_released and event.event_id not in self._live_events:
            # Replay is parked on the fleet, and it is released by draining
            # rather than by calling back, so nothing here has died.
            return True
        return self._has_live_owner(event.event_id)

    async def _run_event(self, event: JournalEvent) -> bool:
        """Run one journal event's callback and report whether it may settle.

        True means the event's semantic work is over. False means something in
        this process still owns it, so the row stays pending and the worker
        offers it again. Why the work ended is not part of the answer: nothing
        durable records it and nothing reads it back.

        There is no "has this turn finished?" question here any more. A source
        leaves the journal when its answer is durably owed to a room, and a
        turn that owes no answer settles through the intentionally-ignored
        path. Asking `TurnStore` was the duplicate execution authority the
        journal was meant to remove, and it answered the wrong question: a turn
        can be terminal with nothing durable behind it.
        """
        if (
            event.kind in TURN_BACKED_KINDS
            and not self._turn_replay_released
            and event.event_id not in self._live_events
        ):
            # A turn replayed from a previous process needs responders that may
            # not exist yet. Live events are unaffected: their responders are
            # whatever is running now.
            return False
        if event.kind in TURN_BACKED_KINDS and self._has_live_owner(event.event_id):
            # A coalescing batch or a running turn already holds this source
            # and will hand it back. Starting a second turn on it does not
            # answer twice, but the loser of the claim blocks until the winner
            # settles, and it blocks holding the room's lane. Returning here
            # leaves the source deferred, which is what it already was.
            return False
        live = self._live_events.pop(event.event_id, None)
        # An event the journal loaded rather than nio just delivered is a
        # replay. Turn work behaves differently there: it defers silently
        # instead of telling the user an agent is still starting, because that
        # notice was already sent — or the conversation has moved on — by the
        # time a replay runs.
        replaying = live is None
        room, matrix_event = live if live is not None else (None, None)
        if matrix_event is None:
            try:
                matrix_event = parse_journal_event(event)
            except JournalCorruptionError:
                logger.exception(
                    "journal_event_unreplayable",
                    event_id=event.event_id,
                    kind=event.kind.value,
                    room_id=event.room_id,
                )
                return True
        if room is None:
            room = self.room_for_id(event.room_id)
        with turn_dispatch_recovery_scope(active=replaying and event.kind in TURN_BACKED_KINDS):
            return await self._invoke(event, room, matrix_event)

    async def _invoke(
        self,
        event: JournalEvent,
        room: nio.MatrixRoom,
        matrix_event: nio.Event,
    ) -> bool:
        """Dispatch to the one callback that owns this event's kind."""
        binding = _BINDINGS.get(event.kind)
        if binding is None or not isinstance(matrix_event, binding.event_types):
            # The stored kind and the payload disagree, which means the payload
            # is not the event that was admitted. Nothing can run -- but the
            # journal has just dropped work it accepted, so this is a
            # corruption report like `journal_event_unreplayable` above, not a
            # routine outcome. It was silent once, and an `m.emote` admitted as
            # actionable work fell through it into nothing for a whole release
            # with no line anywhere saying a message had been discarded.
            logger.error(
                "journal_event_kind_mismatch",
                event_id=event.event_id,
                kind=event.kind.value,
                room_id=event.room_id,
                payload_type=type(matrix_event).__name__,
            )
            return True
        token = _RUNNING_EVENT.set(event)
        try:
            return await binding.run(self, room, matrix_event)
        finally:
            _RUNNING_EVENT.reset(token)

    def semantic_consumer(self) -> SemanticConsumer | None:
        """Return the durable consumer already claimed for the running event."""
        event = _RUNNING_EVENT.get()
        return None if event is None else event.semantic_consumer

    async def claim_semantic_consumer(self, consumer: SemanticConsumer) -> None:
        """Freeze the running event's consumer before it acts on it."""
        event = _RUNNING_EVENT.get()
        if event is None:
            msg = "A semantic consumer can only be claimed inside a journal callback"
            raise RuntimeError(msg)
        claimed = await self.store.claim_semantic_consumer(event.event_id, consumer)
        if claimed is not consumer:
            msg = f"Journal event is already owned by {claimed.value!r}"
            raise RuntimeError(msg)
        _RUNNING_EVENT.set(replace(event, semantic_consumer=consumer))

    async def receipt_order(self) -> int:
        """Return the durable admission order of the running event."""
        event = _RUNNING_EVENT.get()
        if event is None:
            msg = "Receipt order is only available inside a journal callback"
            raise RuntimeError(msg)
        return event.receipt_order

    def release_delivered_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Forget sources the outbox has taken over, after their commit.

        The durable half of contract 2's handoff belongs to the transaction
        that recorded the answer: settling separately would leave a window in
        which a crash left the journal and the outbox both owning the turn,
        and the replay that follows would spend the model a second time on a
        question already answered. What is left here is the in-memory half --
        the worker still lists these events as deferred to a turn that has now
        ended, and nothing else would ever clear them.
        """
        self._worker.release(event_ids)

    async def settle_intentionally_ignored_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Settle turn-backed events that produced no dispatch payload."""
        self._worker.release(event_ids)
        await self.store.settle_many(event_ids)

    def retry_turn_source(self, event_id: str) -> None:
        """Return one undelivered turn source to the worker."""
        self.retry_turn_sources((event_id,))

    def retry_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Return several undelivered turn sources to the worker."""
        self._worker.release(event_ids)
        self._worker.wake()

    async def unsettled_event_ids(self) -> frozenset[str]:
        """Return every event that still owes semantic work."""
        return await self.store.unsettled_event_ids()

    async def unsettled_room_lifecycle_member_ids(self) -> frozenset[tuple[str, str]]:
        """Return room and member identities still owned by lifecycle events.

        Every one of them, walked to the end rather than read as one page. The
        caller records the joins this set does not cover as already seen, so an
        identity left out because a page filled up is a join hook that never
        runs and nothing ever asks about again.

        A row the store could not read is that same hole arriving by a
        different route, and it is the more dangerous one because the walk
        would finish cleanly around it. There is no partial answer to give
        here: the one thing this set is used for is deciding what may be
        written off, so a walk that skipped a row refuses rather than
        under-reporting. A lifecycle row whose payload is not a member event
        already fails the same way two lines down.
        """
        members: set[tuple[str, str]] = set()
        cursor: int | None = None
        while True:
            page = await self.store.pending_of_kind(
                EventKind.ROOM_LIFECYCLE,
                limit=_LIFECYCLE_PAGE_SIZE,
                after_receipt_order=cursor,
            )
            if page.unreadable_rows:
                msg = (
                    f"{page.unreadable_rows} unsettled room lifecycle row(s) could not be read, "
                    "so the identities still owing a hook cannot be enumerated"
                )
                raise JournalCorruptionError(msg)
            for event in page:
                parsed = parse_journal_event(event)
                if not isinstance(parsed, nio.RoomMemberEvent):
                    msg = f"Room lifecycle event {event.event_id!r} is not a member event"
                    raise JournalCorruptionError(msg)
                members.add((event.room_id, parsed.state_key))
            if page.reached_end:
                return frozenset(members)
            cursor = page.resume_after


@dataclass(frozen=True, slots=True)
class _Binding:
    """The event types one kind accepts, and what to run for them."""

    event_types: type | tuple[type, ...]
    run: Callable[[JournalDispatcher, nio.MatrixRoom, Any], Awaitable[bool]]


def _turn_backed(
    callback: Callable[[JournalCallbacks], Callable[[nio.MatrixRoom, Any], Awaitable[TurnDispatchOutcome]]],
) -> Callable[[JournalDispatcher, nio.MatrixRoom, Any], Awaitable[bool]]:
    """Wrap a callback whose work may outlive it inside a turn.

    Neither of these two asks whether someone already owns the source. That is
    the same question for both of them, so ``_run_event`` asks it once for the
    kind rather than each of them answering it for itself -- which is how the
    media path came to have a guard the message path did not.
    """

    async def run(
        dispatcher: JournalDispatcher,
        room: nio.MatrixRoom,
        event: Any,  # noqa: ANN401 - the binding already checked the type
    ) -> bool:
        return _turn_settles(await callback(dispatcher.callbacks)(room, event))

    return run


def _completing(
    callback: Callable[[JournalCallbacks], Callable[[nio.MatrixRoom, Any], Awaitable[None]]],
) -> Callable[[JournalDispatcher, nio.MatrixRoom, Any], Awaitable[bool]]:
    """Wrap a callback whose work is finished when it returns."""

    async def run(
        dispatcher: JournalDispatcher,
        room: nio.MatrixRoom,
        event: Any,  # noqa: ANN401 - the binding already checked the type
    ) -> bool:
        await callback(dispatcher.callbacks)(room, event)
        return True

    return run


_BINDINGS: dict[EventKind, _Binding] = {
    EventKind.MESSAGE: _Binding(TEXTUAL_MESSAGE_EVENT_TYPE, _turn_backed(lambda c: c.on_message)),
    EventKind.MEDIA: _Binding(MATRIX_MEDIA_EVENT_TYPES, _turn_backed(lambda c: c.on_media)),
    EventKind.REACTION: _Binding(nio.ReactionEvent, _completing(lambda c: c.on_reaction)),
    EventKind.APPROVAL: _Binding(nio.UnknownEvent, _completing(lambda c: c.on_approval)),
    EventKind.ROOM_LIFECYCLE: _Binding(nio.RoomMemberEvent, _completing(lambda c: c.on_room_lifecycle)),
    EventKind.REDACTION: _Binding(nio.RedactionEvent, _completing(lambda c: c.on_redaction)),
    EventKind.DECRYPTION_FAILURE: _Binding(nio.MegolmEvent, _completing(lambda c: c.on_decryption_failure)),
}


def _turn_settles(outcome: TurnDispatchOutcome) -> bool:
    """Translate a turn callback's report into a settlement decision."""
    if outcome is TurnDispatchOutcome.DEFERRED:
        return False
    if outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED:
        return True
    msg = f"Turn callback returned invalid outcome {outcome!r}"
    raise TypeError(msg)


__all__ = [
    "TURN_BACKED_KINDS",
    "JournalCallbacks",
    "JournalDispatcher",
]
