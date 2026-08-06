"""Turning admitted journal events into the typed Matrix callbacks MindRoom has.

The journal owns what was accepted and what still owes work. This owns the
fan-out: which callback runs for an event, whether the callback finished the
work or handed it to a turn, and who is allowed to consume a reaction that
several features could each claim.

The important asymmetry is between callbacks that finish and callbacks that
defer. A reaction is done when its handler returns. A message is not: it enters
coalescing and a turn, which may still be running long after the callback
returns. So a deferring handler leaves its event pending, and the turn settles
it when the turn becomes terminal. That is why a crash mid-turn replays the
message rather than losing the answer.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import nio

from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.event_journal import EventKind, SemanticConsumer, SettlementOutcome
from mindroom.logging_config import get_logger
from mindroom.matrix.journal_ingress import (
    JournalCorruptionError,
    JournalIngress,
    event_kind,
    inbound_event,
    parse_journal_event,
    projected_event,
)
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, MatrixMediaEvent
from mindroom.pending_event_worker import PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import EventClass, PrincipalStore

from mindroom.event_journal import JournalEvent  # noqa: TC001 - a runtime ContextVar parameter

logger = get_logger(__name__)

# The journal event whose callback is executing on this task. Callbacks reach
# it to claim a consumer or read their receipt order without every one of them
# having to thread the event through its own signature.
_RUNNING_EVENT: ContextVar[JournalEvent | None] = ContextVar("running_journal_event", default=None)

# Kinds whose work outlives its callback, because the callback only starts a
# turn. Their events stay pending until the turn is terminal.
TURN_BACKED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA})

type MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[TurnDispatchOutcome]]
type MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[TurnDispatchOutcome]]
type ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
type ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
type RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
type RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
type DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]


def event_kind_for_source_kind(source_kind: str) -> EventKind:
    """Return the journal kind that owns one coalescing source kind."""
    if source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND}:
        return EventKind.MEDIA
    return EventKind.MESSAGE


@dataclass(frozen=True, slots=True)
class JournalCallbacks:
    """The typed Matrix callbacks the journal dispatches to."""

    on_message: MessageCallback
    on_media: MediaCallback
    on_reaction: ReactionCallback
    on_approval: ApprovalCallback
    on_room_lifecycle: RoomLifecycleCallback
    on_redaction: RedactionCallback
    on_decryption_failure: DecryptionFailureCallback
    source_has_live_owner: Callable[[str], bool]


@dataclass
class JournalDispatcher:
    """Admit Matrix events durably, then run their callbacks from the journal."""

    store: PrincipalStore
    callbacks: JournalCallbacks
    room_for_id: Callable[[str], nio.MatrixRoom]
    turn_is_terminal: Callable[[str], bool]
    on_persist_failure: Callable[[], None] | None = None
    room_lifecycle_admission_enabled: Callable[[], bool] = lambda: False
    background_task_owner: object | None = None
    _worker: PendingEventWorker = field(init=False, repr=False)
    _ingress: JournalIngress = field(init=False, repr=False)
    # Events whose turn is running in this process. They stay pending durably
    # so a crash replays them, but redispatching them while the turn is alive
    # would answer the same message twice.
    _handed_off: set[str] = field(default_factory=set, init=False, repr=False)
    # The event objects nio already parsed, kept until their callback runs.
    # Replaying from the stored payload is what recovery is for; doing it for
    # an event that is still in hand would parse every event twice and discard
    # the decryption state nio attached to the original.
    _live_events: dict[str, tuple[nio.MatrixRoom, nio.Event]] = field(default_factory=dict, init=False, repr=False)
    # Turn-backed events whose turn reported itself terminal. Recorded by
    # whichever thread finished the turn and settled by the worker.
    _terminal_sources: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the worker and admission adapter this dispatcher owns."""
        self._worker = PendingEventWorker(store=self.store, handle=self._run_event)
        self._ingress = JournalIngress(
            store=self.store,
            on_admitted=self._worker.wake,
            room_lifecycle_enabled=self.room_lifecycle_admission_enabled,
            on_event_admitted=self._remember_live_event,
        )

    def _remember_live_event(self, room: nio.MatrixRoom, event: nio.Event) -> None:
        """Keep the room and event nio already produced, for their callback."""
        self._live_events[event.event_id] = (room, event)

    def register(self, client: nio.AsyncClient) -> None:
        """Install durable admission ahead of every other callback."""
        self._ingress.register(client)

    def start(self) -> None:
        """Begin draining, including work a previous process left pending."""
        self._worker.start()

    def wake(self) -> None:
        """Signal that newly admitted work is waiting."""
        self._worker.wake()

    def ingress_admission_kind(self, event: nio.Event) -> EventKind | None:
        """Return the kind timeline admission would give one event, if any."""
        return self._ingress.admission_kind(event)

    async def stop(self) -> None:
        """Stop draining, leaving unfinished work pending for the next start."""
        await self._worker.stop()

    async def drain_once(self) -> int:
        """Run everything currently pending to completion.

        This is the explicit recovery entry point, so it also forgets which
        events it had handed to a turn. A turn that deferred without taking
        ownership — the router declining an unready candidate, say — would
        otherwise never be reconsidered. Duplicate turns are prevented by
        `TurnStore` claiming its sources, not by this bookkeeping.
        """
        self._handed_off.clear()
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
                projected_event(room.room_id, event, kind),
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
            return
        outcome = await self._run_event(stored)
        if outcome is not None:
            await self.store.settle(event.event_id, outcome)

    async def _run_event(self, event: JournalEvent) -> SettlementOutcome | None:
        """Run one journal event's callback and report how it settled."""
        if event.kind in TURN_BACKED_KINDS and (
            event.event_id in self._terminal_sources or self.turn_is_terminal(event.event_id)
        ):
            # Checked before the in-flight guard on purpose. A turn that has
            # already reported itself terminal owes nothing more, and running
            # its callback again would ask the turn engine to redo work it has
            # finished.
            self._handed_off.discard(event.event_id)
            self._terminal_sources.discard(event.event_id)
            return SettlementOutcome.SUCCEEDED
        if event.event_id in self._handed_off:
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
            outcome = await self._invoke(event, room, matrix_event)
        if outcome is None:
            self._handed_off.add(event.event_id)
        return outcome

    async def _invoke(
        self,
        event: JournalEvent,
        room: nio.MatrixRoom,
        matrix_event: nio.Event,
    ) -> SettlementOutcome | None:
        """Dispatch to the one callback that owns this event's kind."""
        token = _RUNNING_EVENT.set(event)
        try:
            match event.kind:
                case EventKind.MESSAGE:
                    if not isinstance(matrix_event, nio.RoomMessageText):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    return _turn_outcome(await self.callbacks.on_message(room, matrix_event))
                case EventKind.MEDIA:
                    if not isinstance(matrix_event, MATRIX_MEDIA_EVENT_TYPES):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    if self.callbacks.source_has_live_owner(matrix_event.event_id):
                        return None
                    return _turn_outcome(await self.callbacks.on_media(room, matrix_event))
                case EventKind.REACTION:
                    if not isinstance(matrix_event, nio.ReactionEvent):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    await self.callbacks.on_reaction(room, matrix_event)
                case EventKind.APPROVAL:
                    if not isinstance(matrix_event, nio.UnknownEvent):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    await self.callbacks.on_approval(room, matrix_event)
                case EventKind.ROOM_LIFECYCLE:
                    if not isinstance(matrix_event, nio.RoomMemberEvent):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    await self.callbacks.on_room_lifecycle(room, matrix_event)
                case EventKind.REDACTION:
                    if not isinstance(matrix_event, nio.RedactionEvent):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    await self.callbacks.on_redaction(room, matrix_event)
                case EventKind.DECRYPTION_FAILURE:
                    if not isinstance(matrix_event, nio.MegolmEvent):
                        return SettlementOutcome.INTENTIONALLY_IGNORED
                    await self.callbacks.on_decryption_failure(room, matrix_event)
                case EventKind.INVITE:
                    return SettlementOutcome.INTENTIONALLY_IGNORED
        finally:
            _RUNNING_EVENT.reset(token)
        return SettlementOutcome.SUCCEEDED

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

    def release_terminal_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Hand turn-backed events back to the worker once their turn is terminal.

        Called from wherever the turn became durable, which may be a worker
        thread, so this only mutates in-memory state and does no I/O. The
        worker settles them on its own loop. Settling here would mean
        scheduling a coroutine from an arbitrary thread onto a loop nothing
        awaits, which is how a finished turn ends up looking pending forever.
        """
        self._handed_off.difference_update(event_ids)
        self._terminal_sources.update(event_ids)

    async def settle_intentionally_ignored_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Settle turn-backed events that produced no dispatch payload."""
        self._handed_off.difference_update(event_ids)
        self._terminal_sources.difference_update(event_ids)
        await self.store.settle_many(event_ids, SettlementOutcome.INTENTIONALLY_IGNORED)

    def retry_turn_source(self, event_id: str) -> None:
        """Return one undelivered turn source to the worker."""
        self._handed_off.discard(event_id)
        self._terminal_sources.discard(event_id)
        self._worker.wake()

    def retry_turn_sources(self, event_ids: tuple[str, ...]) -> None:
        """Return several undelivered turn sources to the worker."""
        for event_id in event_ids:
            self._handed_off.discard(event_id)
            self._terminal_sources.discard(event_id)
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
