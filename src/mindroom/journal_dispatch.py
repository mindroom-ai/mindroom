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
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.event_journal import EventKind, SemanticConsumer, SettlementOutcome
from mindroom.logging_config import get_logger
from mindroom.matrix.journal_ingress import (
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

# Kinds whose work outlives its callback, because the callback only starts a
# turn. Their events stay pending until that turn's answer is durably owed.
TURN_BACKED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA})

type _MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[TurnDispatchOutcome]]
type _MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[TurnDispatchOutcome]]
type _ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
type _ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
type _RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
type _RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
type _DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]


def event_kind_for_source_kind(source_kind: str) -> EventKind:
    """Return the journal kind that owns one coalescing source kind."""
    if source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND}:
        return EventKind.MEDIA
    return EventKind.MESSAGE


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
    cache_historical_event: Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]] | None = None
    # Replaying a turn needs the agent fleet up, so the orchestrator releases
    # turn-backed replay separately from the rest of startup. Until it does,
    # those events stay pending; everything else drains immediately.
    _turn_replay_released: bool = field(default=False, init=False, repr=False)
    background_task_owner: object | None = None
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
        )
        self._ingress = JournalIngress(
            store=self.store,
            self_sender=self.self_sender,
            on_admitted=self._worker.wake,
            room_lifecycle_enabled=self.room_lifecycle_admission_enabled,
            on_event_admitted=self._remember_live_event,
            on_persist_failure=self.on_persist_failure or (lambda: None),
            **(
                {"cache_historical_event": self.cache_historical_event}
                if self.cache_historical_event is not None
                else {}
            ),
        )

    def _remember_live_event(self, room: nio.MatrixRoom, event: nio.Event) -> None:
        """Keep the room and event nio already produced, for their callback."""
        self._live_events[event.event_id] = (room, event)

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

    def ingress_admission_kind(self, event: nio.Event) -> EventKind | None:
        """Return the kind timeline admission would give one event, if any."""
        return self._ingress.admission_kind(event)

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

        This is the explicit recovery entry point, so it releases turn replay
        and treats nothing as in flight. A turn that deferred without taking
        ownership — the router declining an unready candidate, say — would
        otherwise never be reconsidered. Duplicate turns are prevented by
        `TurnStore` claiming its sources, not by this bookkeeping.
        """
        self._turn_replay_released = True
        self._worker.forget_all_deferrals()
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
            await self.store.admit(
                inbound_event(room.room_id, event, kind, event_class),
                projected_event(room.room_id, event, kind, self_sender=self.self_sender),
            )
        except Exception:
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        if live:
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
        """
        await self.admit_out_of_band(room, event, kind, event_class)
        stored = await self.store.load_event(event.event_id)
        if stored is None or not await self.store.is_pending(event.event_id):
            # A context-only event is admitted already settled, so no callback
            # will ever run for it. Keeping the parsed object would hold it for
            # a run that cannot come.
            self._live_events.pop(event.event_id, None)
            return
        outcome = await self._run_event(stored)
        if outcome is not None:
            await self.store.settle(event.event_id, outcome)

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
        return self.callbacks.source_has_live_owner(event.event_id) or self.callbacks.turn_has_live_claim(
            event.event_id,
        )

    async def _run_event(self, event: JournalEvent) -> SettlementOutcome | None:
        """Run one journal event's callback and report how it settled.

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
            return None
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
                return SettlementOutcome.INTENTIONALLY_IGNORED
        if room is None:
            room = self.room_for_id(event.room_id)
        with turn_dispatch_recovery_scope(active=replaying and event.kind in TURN_BACKED_KINDS):
            return await self._invoke(event, room, matrix_event)

    async def _invoke(
        self,
        event: JournalEvent,
        room: nio.MatrixRoom,
        matrix_event: nio.Event,
    ) -> SettlementOutcome | None:
        """Dispatch to the one callback that owns this event's kind."""
        binding = _BINDINGS.get(event.kind)
        if binding is None or not isinstance(matrix_event, binding.event_types):
            # The stored kind and the replayed event disagree, which means the
            # payload is not the event that was admitted. Nothing can run.
            return SettlementOutcome.INTENTIONALLY_IGNORED
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
        await self.store.settle_many(event_ids, SettlementOutcome.INTENTIONALLY_IGNORED)

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
        """Return room and member identities still owned by lifecycle events."""
        members: set[tuple[str, str]] = set()
        for event in await self.store.pending_of_kind(EventKind.ROOM_LIFECYCLE):
            parsed = parse_journal_event(event)
            if not isinstance(parsed, nio.RoomMemberEvent):
                msg = f"Room lifecycle event {event.event_id!r} is not a member event"
                raise JournalCorruptionError(msg)
            members.add((event.room_id, parsed.state_key))
        return frozenset(members)


@dataclass(frozen=True, slots=True)
class _Binding:
    """The event types one kind accepts, and what to run for them."""

    event_types: type | tuple[type, ...]
    run: Callable[[JournalDispatcher, nio.MatrixRoom, Any], Awaitable[SettlementOutcome | None]]


async def _run_message(
    dispatcher: JournalDispatcher,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
) -> SettlementOutcome | None:
    return _turn_outcome(await dispatcher.callbacks.on_message(room, event))


async def _run_media(
    dispatcher: JournalDispatcher,
    room: nio.MatrixRoom,
    event: MatrixMediaEvent,
) -> SettlementOutcome | None:
    if dispatcher.callbacks.source_has_live_owner(event.event_id):
        # The coalescing gate still owns this source and will hand it back.
        return None
    return _turn_outcome(await dispatcher.callbacks.on_media(room, event))


def _completing(
    callback: Callable[[JournalCallbacks], Callable[[nio.MatrixRoom, Any], Awaitable[None]]],
) -> Callable[[JournalDispatcher, nio.MatrixRoom, Any], Awaitable[SettlementOutcome | None]]:
    """Wrap a callback whose work is finished when it returns."""

    async def run(
        dispatcher: JournalDispatcher,
        room: nio.MatrixRoom,
        event: Any,  # noqa: ANN401 - the binding already checked the type
    ) -> SettlementOutcome | None:
        await callback(dispatcher.callbacks)(room, event)
        return SettlementOutcome.SUCCEEDED

    return run


_BINDINGS: dict[EventKind, _Binding] = {
    EventKind.MESSAGE: _Binding(nio.RoomMessageText, _run_message),
    EventKind.MEDIA: _Binding(MATRIX_MEDIA_EVENT_TYPES, _run_media),
    EventKind.REACTION: _Binding(nio.ReactionEvent, _completing(lambda c: c.on_reaction)),
    EventKind.APPROVAL: _Binding(nio.UnknownEvent, _completing(lambda c: c.on_approval)),
    EventKind.ROOM_LIFECYCLE: _Binding(nio.RoomMemberEvent, _completing(lambda c: c.on_room_lifecycle)),
    EventKind.REDACTION: _Binding(nio.RedactionEvent, _completing(lambda c: c.on_redaction)),
    EventKind.DECRYPTION_FAILURE: _Binding(nio.MegolmEvent, _completing(lambda c: c.on_decryption_failure)),
}


def _turn_outcome(outcome: TurnDispatchOutcome) -> SettlementOutcome | None:
    """Translate a turn callback's report into a settlement decision."""
    if outcome is TurnDispatchOutcome.DEFERRED:
        return None
    if outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED:
        return SettlementOutcome.INTENTIONALLY_IGNORED
    msg = f"Turn callback returned invalid outcome {outcome!r}"
    raise TypeError(msg)


__all__ = [
    "TURN_BACKED_KINDS",
    "JournalCallbacks",
    "JournalDispatcher",
    "event_kind_for_source_kind",
]
