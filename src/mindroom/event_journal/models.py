"""Typed values crossing the event-journal boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
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


# Kinds whose work outlives its callback, because the callback only starts a
# turn. Their events stay pending until that turn's answer is durably owed.
#
# This lives beside the kinds rather than with the dispatcher that acts on
# them, because the journal's own reads need it too: a replay guard asking
# "is there newer unfinished work here" means work that can still answer, and
# pending alone does not mean that. Thread membership is derived from content
# for every kind alike, so a pending reaction, approval, or undecryptable
# message can sit in a thread and be mistaken for an unanswered turn.
TURN_BACKED_KINDS = frozenset({EventKind.MESSAGE, EventKind.MEDIA})


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
    # The same departure observed again, by either observer, with no rejoin in
    # between for a second departure to have happened in.
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
    """One admitted event replayed from the journal.

    Carries neither the class that decided whether it was actionable nor the
    membership it was admitted under. Both are settled at admission and read
    from the row by the journal itself -- the class becomes the ``pending``
    state, and the epoch is asked for by ``admitted_membership_epoch`` when a
    delivery is fenced. Replaying them beside the event would offer a consumer
    a second, staler way to ask the same questions.
    """

    event_id: str
    room_id: str
    thread_id: str | None
    kind: EventKind
    sender: str
    origin_server_ts: int
    source: Mapping[str, object]
    receipt_order: int
    semantic_consumer: SemanticConsumer | None = None


# A non-empty ``__slots__`` is a TypeError on a subtype of a variable-length
# built-in, so SLOT001 cannot be satisfied by a tuple that carries anything.
class PendingPage(tuple[JournalEvent, ...]):  # noqa: SLOT001
    """One bounded pass over the pending backlog, and where that pass stopped.

    A page *is* the events it found, which is the only thing most callers ever
    wanted, so it is the tuple of them rather than a wrapper around one. What a
    length cannot say is the rest: whether stopping meant the backlog ended or
    the pass ran out of rows it was allowed to look at, and which row the next
    pass should start behind.

    ``resume_after`` is the last raw row examined, not the last event returned.
    A row whose payload cannot be read is dropped from the result but still
    consumed, so a resume point taken from the events would step back onto that
    row on every pass and never get past a stretch of them.
    """

    resume_after: int | None
    reached_end: bool
    unreadable_rows: int

    def __new__(
        cls,
        events: tuple[JournalEvent, ...],
        *,
        resume_after: int | None,
        reached_end: bool,
        unreadable_rows: int,
    ) -> PendingPage:
        """Return one page of pending events alongside how its scan ended."""
        page = super().__new__(cls, events)
        page.resume_after = resume_after
        page.reached_end = reached_end
        page.unreadable_rows = unreadable_rows
        return page


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


class HydrationPolicy(IntEnum):
    """A named set of bounds a hydration walk may run under, ordered by cost.

    A walk is limited by three separate ceilings -- logical messages, fetched
    events, and requests -- and a caller is defined by all three of them
    together. Recording only one of the numbers made two policies that differ
    on either of the others indistinguishable, which happens to be harmless
    today because MindRoom's two callers move on every axis at once, and stops
    being harmless the moment a third policy exists or one ceiling is raised
    alone: a walk that stopped under a narrower event budget would look like it
    had already discharged a caller needing a wider one.

    So the durable record names the policy rather than measuring it, and the
    member value is the rank that orders one policy against another. Values are
    spaced so a policy can be added between two existing ones without
    renumbering rows already written under them; zero is reserved for "no walk
    of any policy is on record here".
    """

    PROMPT = 10
    EXPORT = 20


@dataclass(frozen=True, slots=True)
class HydrationCoverage:
    """What the walks under one membership have proven about one conversation.

    Two facts rather than one, because a single boolean cannot carry both and
    the two defects that follow from dropping either are mirror images. A walk
    that ran out of conversation proved something permanent about the
    conversation; without recording that, a narrower walk finishing later
    un-says it. A walk that ran out of allowance proved something about the
    policy it ran under; without recording *which* policy, a caller cannot tell
    an untried conversation from one where its own bounds have already been
    spent, and pays for the identical walk on every read forever.

    Neither field says anything about history a skipped sync gap lost. That is
    a fact about the room and it is deliberately kept out of here: it is the
    one truncation no walk can repair, so a walk decision that consulted it
    would re-walk that room forever to reach the same answer.
    """

    # Whether some walk reached the start of the conversation. Monotonic within
    # a membership epoch: it is a fact about the conversation, not about the
    # walk that happened to prove it.
    reached_its_end: bool
    # The rank of the widest-ranked `HydrationPolicy` any walk here has run
    # under, or zero if none has. Also monotonic within an epoch, and read as
    # "a walk under a policy at least this wide has already been tried here".
    #
    # A cost decision and not a guarantee. It does not say that no wider walk
    # would get further, which is false: the servers MindRoom runs against
    # collapse superseded `m.replace` events out of pagination, so the same
    # policy can carry a later walk past a ceiling it hit today. It says only
    # that retrying under a policy no wider than this one is not worth paying
    # for. Completeness is `reached_its_end`, and a caller whose correctness is
    # completeness must read that field and never this one.
    attempted_policy_rank: int


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
    # Whether this row has already been offered to the homeserver. Together
    # with the device below it answers the only question a resend needs: can
    # the frozen transaction ID still collapse onto the event a previous
    # attempt produced?
    attempted: bool = False
    # The device that offered it, or None when none is recorded. A Matrix
    # transaction ID deduplicates within one device, so a row attempted by a
    # device this process is no longer logged in as carries an ID the
    # homeserver would accept as new.
    sending_device_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryAcknowledgement:
    """What one delivery's row names afterwards, and who put it there.

    The two are separate facts and cannot be recovered from each other. Two
    processes can send the same frozen transaction ID from the same device;
    Matrix deduplicates and hands both callers the *same* event ID, while only
    one conditional update binds the row. Comparing the settled event to the
    one just sent therefore tells a loser it won, which is precisely when it
    goes on to publish a record the database does not hold.
    """

    # The event the row names now: this call's if it bound the row, the
    # winner's if it did not, and ``None`` when there is no row left to name
    # one -- a membership fence deleted it between the send and this write.
    settled_event_id: str | None
    # Whether this call's conditional update is the one that bound the row.
    # The only thing that licenses writing anything beside the row.
    bound: bool


@dataclass(frozen=True, slots=True)
class TerminalTurnWrite:
    """One agent's terminal turn record, committed with a delivery acknowledgement.

    The acknowledgement is the proof that a visible answer exists and what its
    event ID is, and that is exactly the fact the turn record needs. Keeping
    them in separate commits leaves a window where a delivered answer has a
    record that does not know its response event -- and an edit of that message
    arriving afterwards is dropped, because there is nothing recorded to edit.
    A startup pass used to rejoin the two; carrying the record into the
    acknowledgement transaction removes the window instead of repairing it.

    Agent-scoped while the acknowledgement is principal-scoped, which is fine
    and is the reason these rows live in the journal's database at all: one
    transaction needs one database, not one scope key.
    """

    agent_name: str
    index_event_ids: tuple[str, ...]
    anchor_event_id: str
    record_json: str
