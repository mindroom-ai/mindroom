"""How a reply turn ended, and the delivery facts its ending reports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from mindroom.approval.failure import cancelled_failure
from mindroom.cancellation import cancel_failure_reason
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.event_journal import ApprovalFailure
from mindroom.reply_lifecycle import ApprovalFailureKind

if TYPE_CHECKING:
    from mindroom.cancellation import CancelSource
    from mindroom.interactive import InteractiveMetadata
    from mindroom.tool_system.events import ToolTraceEntry

_VisibleBodyState = Literal["none", "placeholder_only", "visible_body"]
_VisibleDeliveryKind = Literal["sent", "edited"]


class Suppression(StrEnum):
    """Why a turn answered nothing on purpose; the value is its ``message:cancelled`` reason."""

    # Fires no hook: the turn chose not to take part.
    PARTICIPATION_DECLINED = "participation_declined"
    HOOK = "suppressed_by_hook"
    SILENT_NO_REPORT = "silent_no_report"
    EMPTY_PROMPT = "empty_prompt"
    # A contact turn answers the other side only through matrix_message.
    CONTACT_TURN = "contact_turn"


class FailureCause(StrEnum):
    """Why a turn failed; the value is its ``message:cancelled`` reason."""

    # An exception; its text is the failure's detail.
    ERROR = "error"
    # ``message:before_response`` raised.
    HOOK_FAILED = "hook_failed"
    # Matrix did not take the answer.
    DELIVERY_FAILED = "delivery_failed"
    # The finished turn had nothing to show.
    NOTHING_TO_SHOW = "nothing_to_show"
    # The span no longer owns its reply.
    WRITE_REFUSED = "reply_write_refused"
    # The reply paused for approval but could not show or record the pause.
    APPROVAL_NOT_STARTED = "approval_not_started"


@dataclass(frozen=True, slots=True)
class Completed:
    """The turn's answer is its reply's terminal row."""


@dataclass(frozen=True, slots=True)
class Suppressed:
    """The turn answered nothing on purpose."""

    reason: Suppression


@dataclass(frozen=True, slots=True)
class Cancelled:
    """A Stop, a restart, or another cancellation cut the turn short."""

    source: CancelSource


@dataclass(frozen=True, slots=True)
class Failed:
    """The turn failed; ``detail`` is what failed as a reader may see it, redacted wherever it is shown."""

    cause: FailureCause
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AwaitingApproval:
    """The run paused for approval; its continuation owns the reply and its sources."""


@dataclass(frozen=True, slots=True)
class Owed:
    """The answer's terminal row is recorded but not acknowledged; the outbox owns it."""


type TurnEnding = Completed | Suppressed | Cancelled | Failed | AwaitingApproval | Owed


def approval_failure(ending: Cancelled | Failed | Suppressed) -> ApprovalFailure:
    """Return the failure a paused reply's approval records for a resume that ended this way.

    Only an error carries its detail; every other ending shows its kind's fixed text, never a code.
    """
    match ending:
        case Cancelled(source=source):
            return cancelled_failure(source)
        case Failed(cause=FailureCause.ERROR, detail=detail):
            return ApprovalFailure(ApprovalFailureKind.ERROR, detail)
        case _:
            return ApprovalFailure(ApprovalFailureKind.ERROR)


def hook_failure_reason(ending: TurnEnding) -> str | None:
    """Return the ``message:cancelled`` reason of an ending, or None when the ending fires no such hook."""
    match ending:
        case Cancelled(source=source):
            return cancel_failure_reason(source)
        case Suppressed(reason=Suppression.PARTICIPATION_DECLINED):
            return None
        case Suppressed(reason=reason):
            return reason.value
        case Failed(cause=cause):
            return cause.value
        case _:
            return None


@dataclass(frozen=True)
class StreamTransportOutcome:
    """What a stream that ran to its end left in Matrix; a cancelled or failed stream leaves its span exit the ending."""

    last_physical_stream_event_id: str | None
    rendered_body: str | None
    visible_body_state: _VisibleBodyState
    canonical_final_body_candidate: str | None = None
    # The terminal update did not land.
    failed: bool = False
    interactive_metadata: InteractiveMetadata | None = None

    @property
    def visible_event_id(self) -> str | None:
        """Return the streamed event id only when the stream showed real visible body text."""
        if self.visible_body_state != "visible_body":
            return None
        return self.last_physical_stream_event_id

    @property
    def visible_body_text(self) -> str:
        """Return the current streamed body snapshot used for hook and outcome decisions."""
        return self.rendered_body or ""


@dataclass(frozen=True)
class FinalDeliveryOutcome:
    """How a turn ended, with what its reply shows in Matrix."""

    ending: TurnEnding
    event_id: str | None = None
    is_visible_response: bool = False
    final_visible_body: str | None = None
    delivery_kind: _VisibleDeliveryKind | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: dict[str, Any] | None = None
    interactive_metadata: InteractiveMetadata | None = None

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace or ()))
        object.__setattr__(self, "extra_content", dict(self.extra_content or {}))

    @property
    def completed(self) -> bool:
        """Return whether the turn's answer is its reply's terminal row."""
        return isinstance(self.ending, Completed)

    @property
    def response_run_id(self) -> str | None:
        """Read a valid run identity from the authoritative final delivery payload."""
        metadata = (self.extra_content or {}).get(AI_RUN_METADATA_KEY)
        run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
        return run_id if isinstance(run_id, str) and run_id else None

    @property
    def final_visible_event_id(self) -> str | None:  # noqa: D102
        return self.event_id if self.is_visible_response else None

    @property
    def delivered_substantive_content(self) -> bool:
        """Return whether this outcome proves that nonblank response text reached Matrix."""
        return (
            self.completed
            and self.is_visible_response
            and self.delivery_kind is not None
            and self.final_visible_body is not None
            and bool(self.final_visible_body.strip())
        )

    @property
    def response_text(self) -> str:  # noqa: D102
        return self.final_visible_body or ""

    @property
    def option_map(self) -> dict[str, str] | None:  # noqa: D102
        if self.interactive_metadata is None:
            return None
        return dict(self.interactive_metadata.option_map)

    @property
    def options_list(self) -> tuple[dict[str, str], ...] | None:  # noqa: D102
        if self.interactive_metadata is None:
            return None
        return tuple(dict(item) for item in self.interactive_metadata.options_list)


_ENDING_LABELS: dict[type, str] = {
    Suppressed: "suppressed",
    Cancelled: "cancelled",
    Failed: "error",
    AwaitingApproval: "suspended",
    Owed: "suspended",
}


def outcome_label(outcome: FinalDeliveryOutcome) -> str:
    """Return the pipeline-timing label of how a turn ended."""
    label = _ENDING_LABELS.get(type(outcome.ending))
    if label is not None:
        return label
    if outcome.delivery_kind is not None:
        return outcome.delivery_kind
    return "no_visible_response" if outcome.final_visible_event_id is None else "visible_response_preserved"


@dataclass
class TurnProgress:
    """What one locked turn's attempt got to before it ended, and the ending its settlement decided.

    ``delivery_started`` is set once the attempt began writing its reply; a cancellation cuts the attempt short, so only
    the attempt can say how far it got.
    """

    delivery_started: bool = False
    ending: TurnEnding | None = None

    def note_delivery_started(self, _event_id: str | None = None) -> None:
        """Mark the reply's delivery as begun; the span's reply names the event carrying it."""
        self.delivery_started = True
