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
    ROOM_LIFECYCLE = "room_lifecycle"
    REDACTION = "redaction"
    DECRYPTION_FAILURE = "decryption_failure"


class SettlementOutcome(StrEnum):
    """Terminal outcomes for one journal event's semantic work."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


class SemanticConsumer(StrEnum):
    """The one application consumer that claimed a multi-purpose event.

    A reaction can mean several unrelated things — a stop request, a tool
    approval, an answer to an interactive question — and only one of them may
    act on it. The claim is durable so that a replay after a crash cannot let a
    second consumer also act.
    """

    APPROVAL_REPLY = "approval_reply"
    CONFIG_CONFIRMATION = "config_confirmation"
    TOOL_APPROVAL_REACTION = "tool_approval_reaction"
    STOP_REACTION = "stop_reaction"
    INTERACTIVE_REACTION = "interactive_reaction"
    REACTION_HOOKS = "reaction_hooks"

    @property
    def _event_kind(self) -> EventKind:
        """Return the only event kind allowed to claim this consumer."""
        if self is SemanticConsumer.APPROVAL_REPLY:
            return EventKind.MESSAGE
        return EventKind.REACTION


class AdmissionResult(StrEnum):
    """What durable admission did with one event."""

    ADMITTED = "admitted"
    DUPLICATE = "duplicate"


class DeliveryStage(StrEnum):
    """The delivery points that must survive a crash."""

    INITIAL = "initial"
    FINAL = "final"


class DepartureSource(StrEnum):
    """Which of the two observers of one departure is speaking."""

    # The bot left the room itself, and knows a sync report of it is coming.
    LOCAL = "local"
    # A sync response reported a departure, which may be the report a local
    # departure is owed, or a departure the bot never initiated.
    REPORTED = "reported"


class DepartureObservation(StrEnum):
    """What one observation of a departure did to the room's derived state."""

    FENCED = "fenced"
    # The sync report a local departure was waiting for. Fencing again would
    # delete whatever the membership after it has already built.
    OWED_REPORT_CONSUMED = "owed_report_consumed"
    # The same departure reaching the same observer twice.
    ALREADY_FENCED = "already_fenced"


@dataclass(frozen=True, slots=True)
class DepartureOutcome:
    """What one durably applied departure observation decided."""

    observation: DepartureObservation
    membership_epoch: int
    owed_reports: int

    @property
    def fenced(self) -> bool:
        """Return whether this observation invalidated the room's derived state."""
        return self.observation is DepartureObservation.FENCED


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
    semantic_consumer: SemanticConsumer | None = None


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
    # The scan key recovery pages on. Without it a pass that fails a whole page
    # re-reads the same page forever and never reaches what is behind it.
    created_at_ns: int
