"""Typed values crossing the event-journal boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class EventClass(StrEnum):
    """Whether an admitted event may start semantic work.

    Derived once, at admission, from nio's per-event provenance. MindRoom never
    recomputes it from cursors, timestamps, or pagination shapes.
    """

    ACTIONABLE = "actionable"
    CONTEXT_ONLY = "context_only"


class EventKind(StrEnum):
    """The one semantic purpose an admitted event carries."""

    MESSAGE = "message"
    MEDIA = "media"
    REACTION = "reaction"
    APPROVAL = "approval"
    INVITE = "invite"
    ROOM_LIFECYCLE = "room_lifecycle"
    REDACTION = "redaction"
    DECRYPTION_FAILURE = "decryption_failure"


class SettlementOutcome(StrEnum):
    """Terminal outcomes for one journal event's semantic work."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


class AdmissionResult(StrEnum):
    """What durable admission did with one event."""

    ADMITTED = "admitted"
    DUPLICATE = "duplicate"


class DeliveryStage(StrEnum):
    """The delivery points that must survive a crash."""

    INITIAL = "initial"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """One Matrix event offered to durable admission.

    Carries no ``principal_id``: the bound store supplies it, so a caller
    cannot admit into another bot's journal.
    """

    event_id: str
    room_id: str
    thread_id: str | None
    kind: EventKind
    event_class: EventClass
    sender: str
    origin_server_ts: int
    source: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """One admitted event replayed from the journal."""

    event_id: str
    room_id: str
    thread_id: str | None
    kind: EventKind
    event_class: EventClass
    sender: str
    origin_server_ts: int
    source: Mapping[str, object]
    receipt_order: int
    membership_epoch: int


@dataclass(frozen=True, slots=True)
class VisibleMessage:
    """The latest visible revision of one logical conversation message."""

    logical_event_id: str
    room_id: str
    thread_id: str | None
    sender: str
    created_ts: int
    revision_event_id: str
    revision_ts: int
    content: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ConversationCursor:
    """A stable position in one conversation's chronological order."""

    created_ts: int
    logical_event_id: str


@dataclass(frozen=True, slots=True)
class RefreshRequest:
    """A logical message whose visible revision must be refetched.

    Produced when the currently visible revision was redacted. The token is the
    redaction's journal receipt order, and a refetch installs its result only
    while that exact token is still current.
    """

    room_id: str
    thread_id: str | None
    logical_event_id: str
    refresh_token: int
    membership_epoch: int


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """One bounded page of a conversation.

    ``messages`` never contains a message whose visible revision was redacted;
    such a message appears in ``refresh_pending`` instead. A caller that must
    not omit content resolves the refresh and reads again, and a caller that
    must not block ignores it. Neither can see the redacted revision.
    """

    messages: tuple[VisibleMessage, ...]
    refresh_pending: tuple[RefreshRequest, ...]
    next_cursor: ConversationCursor | None


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    """One claimed, immutable Matrix delivery."""

    turn_id: str
    stage: DeliveryStage
    room_id: str
    thread_id: str | None
    transaction_id: str
    payload: Mapping[str, object]
    edits_event_id: str | None
    acknowledged_event_id: str | None
