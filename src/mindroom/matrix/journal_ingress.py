"""The one boundary where Matrix events become durable MindRoom facts.

nio decides what is live, recovered, or cold history. This module translates
that decision into whether an event may start work, and commits the event
before telling nio it was accepted. MindRoom never re-derives provenance from
cursors, timestamps, membership repetition, or pagination shapes: those
inferences are what the recovery bugs were made of.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

import nio
from typing_extensions import TypeIs

from mindroom.event_journal import (
    AdmissionResult,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    thread_root,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, parse_matrix_media_event_source
from mindroom.matrix.transport_progress import is_transport_progress_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.event_journal import AdmissionView, JournalEvent

logger = get_logger(__name__)

_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"

# Kinds whose events carry conversation content, and so update the projection.
_PROJECTED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA, EventKind.REDACTION})

# What an `m.room.message` must have parsed to before MindRoom can treat it as
# work: nio's base class for every msgtype that carries a textual body, which
# is `m.text`, `m.emote`, and `m.notice`. Everything else an `m.room.message`
# can become is either media (claimed by an earlier kind rule) or a
# `RoomMessageUnknown`, which has no body at all and is demoted to context by
# `_event_class_for`. `journal_dispatch` binds `EventKind.MESSAGE` to this same
# class, so what admission can hand the message callback and what that callback
# accepts are one statement instead of two that drifted apart.
TEXTUAL_MESSAGE_EVENT_TYPE = nio.RoomMessageFormatted

type _MatrixEvent = nio.Event | nio.InviteEvent


class JournalCorruptionError(RuntimeError):
    """A stored journal payload cannot be replayed without inventing input."""


class _RoomIdEvent(Protocol):
    """A nio event carrying the room its decryption pipeline attached."""

    room_id: str


def _is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is a tool-approval response."""
    return isinstance(event, nio.UnknownEvent) and event.type == _TOOL_APPROVAL_RESPONSE_EVENT_TYPE


# Ordered: the first matching rule owns the event. Media is matched before the
# general message rule because every media class subclasses `RoomMessage` and
# would otherwise be swallowed by it, and both are matched before the approval
# predicate because they are concrete nio classes while an approval is an
# `UnknownEvent` distinguished only by its type string.
_KIND_RULES: tuple[tuple[Callable[[nio.Event], bool], EventKind], ...] = (
    (lambda event: isinstance(event, nio.RedactionEvent), EventKind.REDACTION),
    (lambda event: isinstance(event, nio.ReactionEvent), EventKind.REACTION),
    (lambda event: isinstance(event, MATRIX_MEDIA_EVENT_TYPES), EventKind.MEDIA),
    # Every `m.room.message`, matched at the base class rather than by listing
    # msgtypes. Hydration admits any `m.room.message`, so any msgtype this rule
    # misses makes one conversation read differently depending on whether it
    # was watched or rebuilt -- which is the divergence this projection exists
    # to remove. Enumerating instead of generalizing dropped notices first and
    # then emotes, both found only after they had shipped; `RoomMessageText`,
    # `RoomMessageNotice`, and `RoomMessageEmote` are siblings under
    # `RoomMessage`, so no list of them is self-maintaining. What a message
    # becomes -- work or context -- is `_event_class_for`'s question, not this
    # one's, and which payloads the message callback accepts is
    # `TEXTUAL_MESSAGE_EVENT_TYPE`'s. Generalizing here while `journal_dispatch`
    # still enumerated dropped emotes a second time, one layer further in.
    (lambda event: isinstance(event, nio.RoomMessage), EventKind.MESSAGE),
    (_is_tool_approval_response, EventKind.APPROVAL),
    (lambda event: isinstance(event, nio.MegolmEvent), EventKind.DECRYPTION_FAILURE),
)


def _event_kind(event: nio.Event) -> EventKind | None:
    """Return the single semantic purpose one timeline event carries.

    An event maps to at most one kind, which is what makes "no event may create
    more than one semantic turn" a property of the data rather than a rule
    every call site has to remember.
    """
    for matches, kind in _KIND_RULES:
        if matches(event):
            return kind
    return None


def _event_class_for(provenance: nio.TimelineEventProvenance, event: nio.Event) -> EventClass:
    """Return whether events with this provenance may start semantic work.

    Live and recovered events are both things that happened while this bot was
    a member and has not answered yet. Cold history is context the bot is
    seeing for the first time, and answering it would mean replying to
    conversations that ended long ago.

    A notice is the exception at any provenance. `m.notice` means "automated,
    do not react" in Matrix -- it is why clients suppress notifications for it
    -- so admitting one as work would have agents answering each other's thread
    summaries, their own streaming placeholders, and every bridge relay. They
    are still admitted, because the conversation genuinely contains them and
    because a streamed answer's terminal edit needs the placeholder it lands
    on, but they can only ever be context.

    That subsumes the narrower rule this used to carry for this bot's own
    stream frames: those are notices, so they are covered by being notices.

    A msgtype nio could not type is the other exception, for a plainer reason:
    `RoomMessageUnknown` carries no `body`, so there is no utterance for a turn
    to answer. Demoting the class nio uses for "I do not know this msgtype" is
    not the same mistake as enumerating msgtypes -- the set stays correct as
    Matrix grows -- and it still projects, so the conversation keeps the event.
    """
    if provenance is nio.TimelineEventProvenance.HISTORY:
        return EventClass.CONTEXT_ONLY
    if isinstance(event, nio.RoomMessageNotice | nio.RoomMessageUnknown):
        return EventClass.CONTEXT_ONLY
    return EventClass.ACTIONABLE


def _event_source(event: _MatrixEvent) -> dict[str, object]:
    """Return the exact replay input for one event.

    nio attaches decryption results to the parsed event rather than to its
    source, and pops invite content while parsing, so both are restored here.
    Without them a recovered event would replay as a different, less trusted
    event than the one that was admitted.
    """
    source = dict(event.source)
    source.pop(_SECURITY_METADATA_KEY, None)
    if isinstance(event, nio.Event) and event.decrypted:
        source[_SECURITY_METADATA_KEY] = {
            "decrypted": True,
            "verified": event.verified,
            "sender_key": event.sender_key,
            "session_id": event.session_id,
        }
    if isinstance(event, nio.InviteMemberEvent):
        source["content"] = dict(event.content)
    return source


def inbound_event(
    room_id: str,
    event: nio.Event,
    kind: EventKind,
    event_class: EventClass,
) -> InboundEvent:
    """Return the admission view of one timeline event."""
    content = event.source.get("content")
    return InboundEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content) if isinstance(content, dict) else None,
        kind=kind,
        event_class=event_class,
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        source=_event_source(event),
    )


def projected_event(
    room_id: str,
    event: nio.Event,
    kind: EventKind,
    *,
    self_sender: str,
) -> ProjectedEvent | None:
    """Return the projection view of one event, when it carries content.

    ``self_sender`` is this bot's raw Matrix user ID, which is what a timeline
    event's sender is compared against. It is not the journal principal, whose
    identity also carries the agent name.

    Returning nothing for this bot's own in-flight streaming edit is what keeps
    a streamed answer to one projection write rather than one per progress
    edit. It happens here so that nothing which admits an event can forget to.
    """
    if kind not in _PROJECTED_KINDS:
        return None
    content = event.source.get("content")
    content = content if isinstance(content, dict) else {}
    # nio's schema requires a redaction to name its target, so a redaction that
    # reaches here always has one. Room version 11 moved `redacts` into
    # content, but servers still serve the top-level key over the
    # client-server API, which is what nio parses.
    redacts = event.redacts if isinstance(event, nio.RedactionEvent) else None
    projected = ProjectedEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content),
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        content=content,
        replaces_event_id=None,
        redacts_event_id=redacts,
    )
    if is_transport_progress_revision(projected, self_sender=self_sender):
        return None
    return projected


def parse_journal_event(stored: JournalEvent) -> nio.Event:
    """Rebuild one typed nio event from its stored replay payload."""
    source = dict(stored.source)
    security_metadata = source.pop(_SECURITY_METADATA_KEY, None)
    event = parse_matrix_media_event_source(source) if stored.kind is EventKind.MEDIA else nio.Event.parse_event(source)
    if not isinstance(event, nio.Event) or event.event_id != stored.event_id:
        msg = f"Journal event {stored.event_id!r} does not replay as itself"
        raise JournalCorruptionError(msg)
    if isinstance(event, nio.MegolmEvent):
        event.room_id = stored.room_id
    _restore_security_metadata(event, security_metadata, room_id=stored.room_id, event_id=stored.event_id)
    return event


def _restore_security_metadata(
    event: nio.Event,
    metadata: object,
    *,
    room_id: str,
    event_id: str,
) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        msg = f"Journal event {event_id!r} has corrupt security metadata"
        raise JournalCorruptionError(msg)
    fields = cast("Mapping[str, object]", metadata)
    verified = fields.get("verified")
    sender_key = fields.get("sender_key")
    session_id = fields.get("session_id")
    if (
        fields.get("decrypted") is not True
        or not isinstance(verified, bool)
        or (sender_key is not None and not isinstance(sender_key, str))
        or (session_id is not None and not isinstance(session_id, str))
    ):
        msg = f"Journal event {event_id!r} has corrupt security metadata"
        raise JournalCorruptionError(msg)
    event.decrypted = True
    event.verified = verified
    event.sender_key = sender_key
    event.session_id = session_id
    cast("_RoomIdEvent", event).room_id = room_id


# The provenance of the nio delivery whose callbacks are currently running.
# Some room-state consumers must act only on live activity, and this is the one
# place that fact is known without re-deriving it.
_DELIVERY_PROVENANCE: ContextVar[tuple[str, nio.TimelineEventProvenance] | None] = ContextVar(
    "mindroom_delivery_provenance",
    default=None,
)


def event_is_live(event_id: str) -> bool:
    """Return whether the current nio fan-out belongs to this live event."""
    return _DELIVERY_PROVENANCE.get() == (event_id, nio.TimelineEventProvenance.LIVE)


@dataclass(slots=True)
class TimelineMemberProvenance:
    """What nio said about each room-member event of one sync delivery.

    nio hands provenance to admission once and keeps nothing a later consumer
    can address, so a consumer that runs after the timeline -- the room-member
    join walk, which only sees the response -- can know it only by having been
    told here.

    An entry lives exactly as long as the response that produced it. nio's
    verdict is about one delivery, and answering a later response from a kept
    entry would be re-deriving provenance under another name.

    An event nio already accepted on an earlier pass records nothing at all,
    because nio skips admission for it entirely. That absence is the answer
    rather than a gap to fill: the event is already journaled with its true
    class, and guessing one would settle it against that.
    """

    _recorded: dict[str, nio.TimelineEventProvenance] = field(default_factory=dict)

    def record(self, event_id: str, provenance: nio.TimelineEventProvenance) -> None:
        """Record what nio said about one room-member event."""
        self._recorded[event_id] = provenance

    def get(self, event_id: str) -> nio.TimelineEventProvenance | None:
        """Return nio's verdict for one room-member event, when it gave one."""
        return self._recorded.get(event_id)

    def clear(self) -> None:
        """Forget every verdict, because the response that produced them is done."""
        self._recorded.clear()


@dataclass(slots=True)
class JournalIngress:
    """Commit every inbound Matrix event before nio considers it delivered."""

    store: AdmissionView
    # This bot's raw Matrix user ID, so a self-authored streaming edit can be
    # recognized as transport. Deliberately not the journal principal, which
    # prefixes the agent name and would therefore never match a sender.
    self_sender: str
    on_admitted: Callable[[], None] = lambda: None
    # Room-membership events are only MindRoom's to act on once the router is
    # ready for them, which the timeline callback cannot decide for itself.
    room_lifecycle_enabled: Callable[[], bool] = lambda: False
    on_event_admitted: Callable[[nio.MatrixRoom, nio.Event], None] = lambda _room, _event: None
    # A refused admission must also stop the sync checkpoint advancing past the
    # event, or the next process would never see it again.
    on_persist_failure: Callable[[], None] = lambda: None
    # What nio said about the room-member events of the response being
    # delivered, for the consumers that run once the response is complete.
    timeline_member_provenance: TimelineMemberProvenance = field(
        default_factory=TimelineMemberProvenance,
        init=False,
    )

    def register(self, client: nio.AsyncClient) -> None:
        """Install durable admission ahead of every other callback."""
        client.add_event_admission_callback(self._admit)

    def _admission_kind(self, event: nio.Event) -> EventKind | None:
        """Return the kind this event is admitted as, or nothing."""
        kind = _event_kind(event)
        if kind is None and isinstance(event, nio.RoomMemberEvent) and self.room_lifecycle_enabled():
            return EventKind.ROOM_LIFECYCLE
        return kind

    def timeline_member_event_class(self, event: nio.Event) -> EventClass | None:
        """Return the class nio's provenance gives one member event, if it said.

        Deriving it here rather than at the call site is what stops a consumer
        that runs after the timeline from classifying an event differently than
        admission would have.
        """
        provenance = self.timeline_member_provenance.get(event.event_id)
        if provenance is None:
            return None
        return _event_class_for(provenance, event)

    async def _admit(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        _DELIVERY_PROVENANCE.set((event.event_id, provenance))
        if isinstance(event, nio.RoomMemberEvent):
            # Recorded before anything can decline to admit this event.
            # Declining is exactly when a later consumer needs the verdict:
            # nothing else in the response will have written it down.
            self.timeline_member_provenance.record(event.event_id, provenance)
        kind = self._admission_kind(event)
        if kind is None:
            return
        event_class = _event_class_for(provenance, event)
        try:
            admission = await self.store.admit(
                inbound_event(room.room_id, event, kind, event_class),
                projected_event(room.room_id, event, kind, self_sender=self.self_sender),
            )
        except Exception as error:
            # Refusing acceptance is the whole point: nio keeps the event for
            # redelivery and does not advance the checkpoint past it.
            self.on_persist_failure()
            raise nio.CallbackNotAcceptedError(str(error)) from error
        if event_class is not EventClass.ACTIONABLE:
            return
        if admission is AdmissionResult.ADMITTED:
            # Only an event this admission created can still owe a run, and a
            # run is the only thing that gives the parsed object back. Keeping
            # one for an event the journal already settled means keeping it for
            # a run that cannot come, once per distinct event redelivered from
            # an older checkpoint.
            self.on_event_admitted(room, event)
        self.on_admitted()
