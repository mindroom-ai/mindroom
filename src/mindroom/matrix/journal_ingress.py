"""Classify nio timeline records and reconstruct stored MindRoom events.

Nio decides what is live, recovered, or cold history; this module translates
that provenance into application actionability. Durable batch admission and
acknowledgement belong to matrix/durable_ingestion.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeIs, cast

import nio

from mindroom.constants import ROUTE_EVENT_TYPE, ROUTE_REVISION_KEY, SILENT_SCHEDULE_EVENT_TYPE
from mindroom.event_journal import (
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    thread_root,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.agent_authored import carries_agent_authored_marker
from mindroom.matrix.event_types import CALL_MEMBER_EVENT_TYPE, RTC_NOTIFICATION_EVENT_TYPE
from mindroom.matrix.media import (
    MATRIX_MEDIA_EVENT_TYPES,
    is_encrypted_media_event_source,
    parse_matrix_media_event_source,
)
from mindroom.matrix.transport_progress import is_transport_progress_revision, is_unfinished_reply

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.event_journal import JournalEvent

logger = get_logger(__name__)

_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_BACKGROUND_JOB_CANCEL_EVENT_TYPE = "io.mindroom.background_job_cancel"
_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"
_RTC_EVENT_TYPES = frozenset({CALL_MEMBER_EVENT_TYPE, RTC_NOTIFICATION_EVENT_TYPE})
# Kinds only an account MindRoom manages may send; anyone else's is settled without work.
_MANAGED_SENDER_KINDS = frozenset({EventKind.SCHEDULE_TRIGGER, EventKind.ROUTE})
# Kinds whose events carry conversation content, and so update the projection.
_PROJECTED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA, EventKind.REDACTION})
# Kinds a person sends to ask for something, the only ones the agent-authored marker demotes to context.
_AGENT_AUTHORED_MARKER_KINDS = frozenset(
    {EventKind.MESSAGE, EventKind.MEDIA, EventKind.REACTION, EventKind.APPROVAL, EventKind.JOB_CANCEL},
)

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


def _is_background_job_cancel(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event asks to cancel background jobs."""
    return isinstance(event, nio.UnknownEvent) and event.type == _BACKGROUND_JOB_CANCEL_EVENT_TYPE


def _is_silent_schedule_trigger(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is an internal silent schedule trigger."""
    return isinstance(event, nio.UnknownEvent) and event.type == SILENT_SCHEDULE_EVENT_TYPE


def _is_route(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is a steward's hidden route to the responder it selected."""
    return isinstance(event, nio.UnknownEvent) and event.type == ROUTE_EVENT_TYPE


def _is_rtc_event(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    """Return whether one event is a supported MatrixRTC room event."""
    return isinstance(event, nio.UnknownEvent) and event.type in _RTC_EVENT_TYPES


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
    (_is_silent_schedule_trigger, EventKind.SCHEDULE_TRIGGER),
    (_is_route, EventKind.ROUTE),
    (_is_rtc_event, EventKind.RTC),
    (_is_tool_approval_response, EventKind.APPROVAL),
    (_is_background_job_cancel, EventKind.JOB_CANCEL),
    (lambda event: isinstance(event, nio.MegolmEvent), EventKind.OPAQUE_HISTORY),
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


def _happened_while_member(provenance: nio.TimelineEventProvenance) -> bool:
    """Return whether an event is current: live, or recovered from a gap this bot missed.

    Cold history is the one provenance that is neither. Every provenance rule
    in this module goes through here so the three cannot drift apart.
    """
    return provenance is not nio.TimelineEventProvenance.HISTORY


def _event_class_for(
    provenance: nio.TimelineEventProvenance,
    event: nio.Event,
    kind: EventKind,
    *,
    sender_is_managed: Callable[[str], bool],
) -> EventClass:
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

    A msgtype nio could not type is another exception, for a plainer reason:
    `RoomMessageUnknown` carries no `body`, so there is no utterance for a turn
    to answer. Demoting the class nio uses for "I do not know this msgtype" is
    not the same mistake as enumerating msgtypes -- the set stays correct as
    Matrix grows -- and it still projects, so the conversation keeps the event.

    A text message whose status says it will be revised again is another:
    an agent's `pending` placeholder is `m.text`, and the turn engine never
    answers an unfinished reply, only its terminal edit. Every agent in a room
    receives every agent's placeholder, so admitting them as work queued each
    one in every agent's ordered room lane, ahead of the prompts behind it.

    A message, media, reaction, approval or job cancel marked agent-authored,
    from a sender MindRoom does not manage, is the last one. Such an account
    acts for a person, and the marker says an agent wrote the event there, so
    acting on it would let the agent speak for the person: answer its own
    approval card, run a command with the person's rights, or stop a reply.
    The marker can only take authority away, so its presence decides and its
    value is never read. An entity's own account already speaks only for that
    entity, so the marker takes nothing from it. A message or media event
    still projects, so history keeps it as it is. Every other kind keeps the
    class it would have unmarked: a membership, call or redaction event
    changes the room itself, and its handlers must see that change whoever
    wrote its content.
    """
    if not _happened_while_member(provenance):
        return EventClass.CONTEXT_ONLY
    if isinstance(event, nio.RoomMessageNotice | nio.RoomMessageUnknown):
        return EventClass.CONTEXT_ONLY
    content = event.source.get("content")
    if isinstance(event, TEXTUAL_MESSAGE_EVENT_TYPE) and is_unfinished_reply(content or {}):
        return EventClass.CONTEXT_ONLY
    if (
        kind in _AGENT_AUTHORED_MARKER_KINDS
        and isinstance(content, dict)
        and carries_agent_authored_marker(content)
        and not sender_is_managed(event.sender)
    ):
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


def _inbound_event(
    room_id: str,
    event: nio.Event,
    kind: EventKind,
    event_class: EventClass,
) -> InboundEvent:
    """Return the admission view of one timeline event."""
    content, source = event.source.get("content"), _event_source(event)
    if kind is not EventKind.ROUTE and isinstance(stored := source.get("content"), dict):
        # Which edit a waiting message's text is, only its journal row's revision says: its sender's word is dropped.
        source["content"] = {key: value for key, value in stored.items() if key != ROUTE_REVISION_KEY}
    return InboundEvent(
        event_id=event.event_id,
        room_id=room_id,
        thread_id=thread_root(content) if isinstance(content, dict) else None,
        kind=kind,
        event_class=event_class,
        sender=event.sender,
        origin_server_ts=event.server_timestamp,
        source=source,
    )


def _projected_event(
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
        transaction_id=event.transaction_id,
    )
    if is_transport_progress_revision(projected, self_sender=self_sender):
        return None
    return projected


def _ingested_kind(parsed: nio.Event, sender_is_managed: Callable[[str], bool]) -> EventKind | None:
    """Return the kind one parsed timeline event is admitted as, or None to settle it without work."""
    if parsed.decrypted and (isinstance(parsed, nio.RedactionEvent) or "state_key" in parsed.source):
        # Redactions and state are never encrypted, so a decrypted one is a sender's claim the homeserver never checked.
        return None
    if isinstance(parsed, nio.RedactionEvent) and not parsed.redacts:
        # Nio's schema accepts an empty target, which deletes nothing and which batch validation would reject.
        return None
    kind = _event_kind(parsed)
    if kind in _MANAGED_SENDER_KINDS and not sender_is_managed(parsed.sender):
        return None
    if kind is None and isinstance(parsed, nio.RoomMemberEvent):
        return EventKind.ROOM_LIFECYCLE
    return kind


def ingestion_timeline_views(
    *,
    room_id: str,
    source: Mapping[str, object],
    self_sender: str,
    provenance: nio.TimelineEventProvenance,
    expected_event_id: str | None = None,
    security_metadata: Mapping[str, object] | None = None,
    sender_is_managed: Callable[[str], bool] = lambda _sender: False,
) -> tuple[InboundEvent, ProjectedEvent | None] | None:
    """Classify one durable timeline input, or return its compatibility fate."""
    message = "Unsupported ingestion event"
    parsed = parse_matrix_media_event_source(source) if is_encrypted_media_event_source(source) else None
    if not isinstance(parsed, MATRIX_MEDIA_EVENT_TYPES):
        parsed = nio.Event.parse_event(dict(source))
    if isinstance(parsed, (nio.BadEvent, nio.UnknownBadEvent)):
        # Ordinary malformed timeline payloads are retained by nio. They have
        # no semantic work, but must settle so later input can advance.
        return None
    if not isinstance(parsed, nio.Event):
        raise TypeError(message)
    if expected_event_id is not None and parsed.event_id != expected_event_id:
        raise ValueError(message)
    if security_metadata is not None:
        _restore_security_metadata(parsed, security_metadata, room_id=room_id, event_id=parsed.event_id)
    if isinstance(parsed, nio.MegolmEvent) and provenance is not nio.TimelineEventProvenance.HISTORY:
        # Nio owns ciphertext recovery. Only historical identities need an
        # application tombstone so later decryption cannot revive old work.
        return None
    kind = _ingested_kind(parsed, sender_is_managed)
    if kind is None:
        return None
    event_class = _event_class_for(provenance, parsed, kind, sender_is_managed=sender_is_managed)
    return (
        _inbound_event(room_id, parsed, kind, event_class),
        _projected_event(room_id, parsed, kind, self_sender=self_sender),
    )


def parse_journal_event(stored: JournalEvent) -> nio.Event:
    """Rebuild one typed nio event from its stored replay payload."""
    source = dict(stored.source)
    security_metadata = source.pop(_SECURITY_METADATA_KEY, None)
    # What marks a message as a route is written only by the dispatch of a route event, so no stored event keeps a
    # mark of its own: not beside its content, and not in the content of anything but a route.
    source.pop(ROUTE_EVENT_TYPE, None)
    if stored.kind is not EventKind.ROUTE and isinstance(content := source.get("content"), dict):
        source["content"] = {key: value for key, value in content.items() if key != ROUTE_EVENT_TYPE}
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
