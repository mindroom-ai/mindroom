"""The one boundary where Matrix events become durable MindRoom facts.

nio decides what is live, recovered, or cold history. This module translates
that decision into whether an event may start work, and commits the event
before telling nio it was accepted. MindRoom never re-derives provenance from
cursors, timestamps, membership repetition, or pagination shapes: those
inferences are what the recovery bugs were made of.
"""

from __future__ import annotations

import json
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import JournalEvent, PrincipalStore

logger = get_logger(__name__)

TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"

# Kinds whose events carry conversation content, and so update the projection.
_PROJECTED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA, EventKind.REDACTION})

type MatrixEvent = nio.Event | nio.InviteEvent


class JournalCorruptionError(RuntimeError):
    """A stored journal payload cannot be replayed without inventing input."""


class _RoomIdEvent(Protocol):
    """A nio event carrying the room its decryption pipeline attached."""

    room_id: str


def is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is a tool-approval response."""
    return isinstance(event, nio.UnknownEvent) and event.type == TOOL_APPROVAL_RESPONSE_EVENT_TYPE


def event_kind(event: nio.Event) -> EventKind | None:
    """Return the single semantic purpose one timeline event carries.

    An event maps to at most one kind, which is what makes "no event may create
    more than one semantic turn" a property of the data rather than a rule
    every call site has to remember.
    """
    if isinstance(event, nio.RoomMessageText):
        return EventKind.MESSAGE
    if isinstance(event, nio.RedactionEvent):
        return EventKind.REDACTION
    if isinstance(event, nio.ReactionEvent):
        return EventKind.REACTION
    if isinstance(event, MATRIX_MEDIA_EVENT_TYPES):
        return EventKind.MEDIA
    if is_tool_approval_response(event):
        return EventKind.APPROVAL
    if isinstance(event, nio.MegolmEvent):
        return EventKind.DECRYPTION_FAILURE
    return None


def event_class_for(provenance: nio.TimelineEventProvenance) -> EventClass:
    """Return whether events with this provenance may start semantic work.

    Live and recovered events are both things that happened while this bot was
    a member and has not answered yet. Cold history is context the bot is
    seeing for the first time, and answering it would mean replying to
    conversations that ended long ago.
    """
    if provenance is nio.TimelineEventProvenance.HISTORY:
        return EventClass.CONTEXT_ONLY
    return EventClass.ACTIONABLE


def event_source(event: MatrixEvent) -> dict[str, object]:
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
        source=event_source(event),
    )


def projected_event(room_id: str, event: nio.Event, kind: EventKind) -> ProjectedEvent | None:
    """Return the projection view of one event, when it carries content."""
    if kind not in _PROJECTED_KINDS:
        return None
    content = event.source.get("content")
    content = content if isinstance(content, dict) else {}
    # nio's schema requires a redaction to name its target, so a redaction that
    # reaches here always has one. Room version 11 moved `redacts` into
    # content, but servers still serve the top-level key over the
    # client-server API, which is what nio parses.
    redacts = event.redacts if isinstance(event, nio.RedactionEvent) else None
    return ProjectedEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content),
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        content=content,
        replaces_event_id=None,
        redacts_event_id=redacts,
    )


def parse_journal_event(stored: JournalEvent) -> nio.Event:
    """Rebuild one typed nio event from its stored replay payload."""
    source = dict(stored.source)
    security_metadata = source.pop(_SECURITY_METADATA_KEY, None)
    event = (
        parse_matrix_media_event_source(source)
        if stored.kind is EventKind.MEDIA
        else nio.Event.parse_event(source)
    )
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
    verified = metadata.get("verified")
    sender_key = metadata.get("sender_key")
    session_id = metadata.get("session_id")
    if (
        metadata.get("decrypted") is not True
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
    cast("_RoomIdEvent", event).room_id = room_id  # noqa: TC006


@dataclass(slots=True)
class JournalIngress:
    """Commit every inbound Matrix event before nio considers it delivered."""

    store: PrincipalStore
    on_admitted: Callable[[], None] = lambda: None

    def register(self, client: nio.AsyncClient) -> None:
        """Install durable admission ahead of every other callback."""
        client.add_event_admission_callback(self._admit)

    async def _admit(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        kind = event_kind(event)
        if kind is None:
            return
        event_class = event_class_for(provenance)
        try:
            await self.store.admit(
                inbound_event(room.room_id, event, kind, event_class),
                projected_event(room.room_id, event, kind),
            )
        except Exception as error:
            # Refusing acceptance is the whole point: nio keeps the event for
            # redelivery and does not advance the checkpoint past it.
            raise nio.CallbackNotAcceptedError(str(error)) from error
        if event_class is EventClass.ACTIONABLE:
            self.on_admitted()


def journal_event_json(event: MatrixEvent) -> str:
    """Return the canonical serialization used for durable comparison."""
    return json.dumps(event_source(event), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


type AdmissionCallback = Callable[
    [nio.MatrixRoom, nio.Event, nio.TimelineEventProvenance],
    Awaitable[None],
]
