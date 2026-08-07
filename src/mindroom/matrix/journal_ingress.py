"""The one boundary where Matrix events become durable MindRoom facts.

nio decides what is live, recovered, or cold history. This module translates
that decision into whether an event may start work, and commits the event
before telling nio it was accepted. MindRoom never re-derives provenance from
cursors, timestamps, membership repetition, or pagination shapes: those
inferences are what the recovery bugs were made of.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import nio
from typing_extensions import TypeIs

from mindroom.event_journal import (
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
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.event_journal import AdmissionView, JournalEvent

logger = get_logger(__name__)

_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"

# Kinds whose events carry conversation content, and so update the projection.
_PROJECTED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA, EventKind.REDACTION})

type _MatrixEvent = nio.Event | nio.InviteEvent


async def _ignore_historical_event(_room: nio.MatrixRoom, _event: nio.Event) -> None:
    """Do nothing with a historical event."""


class JournalCorruptionError(RuntimeError):
    """A stored journal payload cannot be replayed without inventing input."""


class _RoomIdEvent(Protocol):
    """A nio event carrying the room its decryption pipeline attached."""

    room_id: str


def _is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is a tool-approval response."""
    return isinstance(event, nio.UnknownEvent) and event.type == _TOOL_APPROVAL_RESPONSE_EVENT_TYPE


# Ordered: the first matching rule owns the event. Media types are checked
# before the approval predicate because they are concrete nio classes, while an
# approval is an `UnknownEvent` distinguished only by its type string.
_KIND_RULES: tuple[tuple[Callable[[nio.Event], bool], EventKind], ...] = (
    (lambda event: isinstance(event, nio.RoomMessageText), EventKind.MESSAGE),
    # A notice is a message. `RoomMessageNotice` is a sibling of
    # `RoomMessageText` rather than a subclass, so leaving it out dropped every
    # notice from live admission while hydration -- which accepts any
    # `m.room.message` -- kept them. One conversation therefore read
    # differently depending on whether it was hydrated or watched, which is the
    # divergence this projection exists to remove. What a notice never becomes
    # is work; see `_event_class_for`.
    (lambda event: isinstance(event, nio.RoomMessageNotice), EventKind.MESSAGE),
    (lambda event: isinstance(event, nio.RedactionEvent), EventKind.REDACTION),
    (lambda event: isinstance(event, nio.ReactionEvent), EventKind.REACTION),
    (lambda event: isinstance(event, MATRIX_MEDIA_EVENT_TYPES), EventKind.MEDIA),
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
    """
    if provenance is nio.TimelineEventProvenance.HISTORY:
        return EventClass.CONTEXT_ONLY
    if isinstance(event, nio.RoomMessageNotice):
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
    # Conversation content that did not arrive live still has to reach the
    # conversation store before admission returns, so a reader that follows
    # this sync response sees the history it was given.
    cache_historical_event: Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]] = _ignore_historical_event
    # A refused admission must also stop the sync checkpoint advancing past the
    # event, or the next process would never see it again.
    on_persist_failure: Callable[[], None] = lambda: None

    def register(self, client: nio.AsyncClient) -> None:
        """Install durable admission ahead of every other callback."""
        client.add_event_admission_callback(self._admit)

    def admission_kind(self, event: nio.Event) -> EventKind | None:
        """Return the kind this event is admitted as, or nothing."""
        kind = _event_kind(event)
        if kind is None and isinstance(event, nio.RoomMemberEvent) and self.room_lifecycle_enabled():
            return EventKind.ROOM_LIFECYCLE
        return kind

    async def _admit(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        _DELIVERY_PROVENANCE.set((event.event_id, provenance))
        if provenance is not nio.TimelineEventProvenance.LIVE:
            try:
                await self.cache_historical_event(room, event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.on_persist_failure()
                raise nio.CallbackNotAcceptedError(str(error)) from error
        kind = self.admission_kind(event)
        if kind is None:
            return
        event_class = _event_class_for(provenance, event)
        try:
            await self.store.admit(
                inbound_event(room.room_id, event, kind, event_class),
                projected_event(room.room_id, event, kind, self_sender=self.self_sender),
            )
        except Exception as error:
            # Refusing acceptance is the whole point: nio keeps the event for
            # redelivery and does not advance the checkpoint past it.
            self.on_persist_failure()
            raise nio.CallbackNotAcceptedError(str(error)) from error
        if event_class is EventClass.ACTIONABLE:
            self.on_event_admitted(room, event)
            self.on_admitted()
