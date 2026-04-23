"""Canonical terminal delivery and policy projection helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from mindroom.tool_system.events import ToolTraceEntry

StreamTerminalOperation = Literal["none", "send", "edit"]
StreamTerminalResult = Literal["not_attempted", "succeeded", "failed", "cancelled"]
TerminalStatus = Literal["completed", "cancelled", "error"]
VisibleBodyState = Literal["none", "placeholder_only", "visible_body"]
VisibleDeliveryKind = Literal["sent", "edited"]
EventIdSource = Literal["none", "final_visible_event", "last_physical_stream_event"]
FinalDeliveryState = Literal[
    "final_visible_delivery",
    "kept_prior_visible_stream_after_completed_terminal_failure",
    "kept_prior_visible_stream_after_cancel",
    "kept_prior_visible_stream_after_error",
    "kept_prior_visible_response_after_suppression",
    "kept_prior_visible_response_after_error",
    "cancelled_with_visible_response",
    "cancelled_with_visible_note",
    "cancelled_without_visible_response",
    "suppressed_without_visible_response",
    "suppressed_redacted",
    "suppression_cleanup_failed",
    "error_with_visible_response",
    "error_without_visible_response",
]

EMPTY_MAPPING = cast("Mapping[str, Any]", MappingProxyType({}))


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return cast(
            "Mapping[str, Any]",
            MappingProxyType({key: _freeze_value(item) for key, item in value.items()}),
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not value:
        return EMPTY_MAPPING
    return cast("Mapping[str, Any]", MappingProxyType({key: _freeze_value(item) for key, item in value.items()}))


def _freeze_string_mapping(value: Mapping[str, str] | None) -> Mapping[str, str] | None:
    if value is None:
        return None
    return cast("Mapping[str, str]", MappingProxyType(dict(value)))


def _freeze_options_list(
    value: Sequence[Mapping[str, str]] | None,
) -> tuple[Mapping[str, str], ...] | None:
    if value is None:
        return None
    return tuple(cast("Mapping[str, str]", MappingProxyType(dict(item))) for item in value)


@dataclass(frozen=True)
class StreamTransportOutcome:
    """Immutable transport facts emitted by streaming finalization."""

    last_physical_stream_event_id: str | None
    terminal_operation: StreamTerminalOperation
    terminal_result: StreamTerminalResult
    terminal_status: TerminalStatus
    rendered_body: str | None
    visible_body_state: VisibleBodyState
    failure_reason: str | None = None
    option_map: Mapping[str, str] | None = None
    options_list: tuple[Mapping[str, str], ...] | None = None

    def __post_init__(self) -> None:
        """Freeze transport metadata and validate one terminal snapshot."""
        object.__setattr__(self, "option_map", _freeze_string_mapping(self.option_map))
        object.__setattr__(self, "options_list", _freeze_options_list(self.options_list))
        if self.terminal_result == "not_attempted" and self.terminal_operation != "none":
            msg = "terminal_operation must be 'none' when terminal_result is 'not_attempted'"
            raise ValueError(msg)
        if self.terminal_result != "not_attempted" and self.terminal_operation == "none":
            msg = "terminal_operation cannot be 'none' when a terminal result exists"
            raise ValueError(msg)
        if self.visible_body_state == "none" and self.rendered_body is not None:
            msg = "visible_body_state 'none' cannot carry a rendered_body"
            raise ValueError(msg)
        if self.visible_body_state != "none" and self.rendered_body is None:
            msg = "visible_body_state requires a rendered_body"
            raise ValueError(msg)

    @property
    def has_any_physical_stream_event(self) -> bool:
        """Return whether streaming ever made a physical event visible."""
        return self.last_physical_stream_event_id is not None

    @property
    def has_rendered_visible_body(self) -> bool:
        """Return whether the rendered body contains real user-visible text."""
        return self.visible_body_state == "visible_body"


@dataclass(frozen=True)
class _StateValidationRule:
    terminal_status: TerminalStatus
    requires_final_visible_event: bool = False
    requires_final_visible_body: bool = False
    allows_final_visible_event: bool = True
    allows_final_visible_body: bool = True
    requires_prior_visible_stream: bool = False
    allows_prior_visible_stream: bool = True


LEGAL_FINAL_DELIVERY_STATES: Mapping[FinalDeliveryState, _StateValidationRule] = MappingProxyType(
    {
        "final_visible_delivery": _StateValidationRule(
            terminal_status="completed",
            requires_final_visible_event=True,
            requires_final_visible_body=True,
        ),
        "kept_prior_visible_stream_after_completed_terminal_failure": _StateValidationRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "kept_prior_visible_stream_after_cancel": _StateValidationRule(
            terminal_status="cancelled",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "kept_prior_visible_stream_after_error": _StateValidationRule(
            terminal_status="error",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "kept_prior_visible_response_after_suppression": _StateValidationRule(
            terminal_status="completed",
            requires_final_visible_event=True,
        ),
        "kept_prior_visible_response_after_error": _StateValidationRule(
            terminal_status="error",
            requires_final_visible_event=True,
        ),
        "cancelled_with_visible_response": _StateValidationRule(
            terminal_status="cancelled",
            requires_final_visible_event=True,
        ),
        "cancelled_with_visible_note": _StateValidationRule(
            terminal_status="cancelled",
            requires_final_visible_event=True,
            requires_final_visible_body=True,
        ),
        "cancelled_without_visible_response": _StateValidationRule(
            terminal_status="cancelled",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
        "suppressed_without_visible_response": _StateValidationRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
        "suppressed_redacted": _StateValidationRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            requires_prior_visible_stream=True,
        ),
        "suppression_cleanup_failed": _StateValidationRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            requires_prior_visible_stream=True,
        ),
        "error_with_visible_response": _StateValidationRule(
            terminal_status="error",
            requires_final_visible_event=True,
        ),
        "error_without_visible_response": _StateValidationRule(
            terminal_status="error",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
    },
)


@dataclass(frozen=True)
class FinalDeliveryPolicy:
    """Single-source policy row for one terminal delivery state."""

    emits_after_response: bool
    emits_cancelled_response: bool
    visible_response_event_source: EventIdSource
    response_identity_event_source: EventIdSource
    turn_completion_event_source: EventIdSource
    should_mark_handled: bool
    retryable: bool
    should_persist_response_identity: bool
    should_queue_thread_summary: bool
    should_register_interactive_follow_up: bool
    should_shield_late_failures: bool


FINAL_DELIVERY_POLICY: Mapping[FinalDeliveryState, FinalDeliveryPolicy] = MappingProxyType(
    {
        "final_visible_delivery": FinalDeliveryPolicy(
            emits_after_response=True,
            emits_cancelled_response=False,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="final_visible_event",
            turn_completion_event_source="final_visible_event",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=True,
            should_queue_thread_summary=True,
            should_register_interactive_follow_up=True,
            should_shield_late_failures=True,
        ),
        "kept_prior_visible_stream_after_completed_terminal_failure": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="last_physical_stream_event",
            response_identity_event_source="last_physical_stream_event",
            turn_completion_event_source="last_physical_stream_event",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=True,
            should_queue_thread_summary=True,
            should_register_interactive_follow_up=True,
            should_shield_late_failures=True,
        ),
        "kept_prior_visible_stream_after_cancel": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="last_physical_stream_event",
            response_identity_event_source="none",
            turn_completion_event_source="last_physical_stream_event",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=True,
        ),
        "kept_prior_visible_stream_after_error": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="last_physical_stream_event",
            response_identity_event_source="last_physical_stream_event",
            turn_completion_event_source="last_physical_stream_event",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=True,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=True,
            should_shield_late_failures=True,
        ),
        "kept_prior_visible_response_after_suppression": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
        "kept_prior_visible_response_after_error": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
        "cancelled_with_visible_response": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="none",
            turn_completion_event_source="final_visible_event",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=True,
        ),
        "cancelled_with_visible_note": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="none",
            turn_completion_event_source="final_visible_event",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=True,
        ),
        "cancelled_without_visible_response": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="none",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
        "suppressed_without_visible_response": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="none",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
        "suppressed_redacted": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="none",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
        "suppression_cleanup_failed": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="last_physical_stream_event",
            response_identity_event_source="none",
            turn_completion_event_source="last_physical_stream_event",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=True,
        ),
        "error_with_visible_response": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="final_visible_event",
            response_identity_event_source="final_visible_event",
            turn_completion_event_source="final_visible_event",
            should_mark_handled=True,
            retryable=False,
            should_persist_response_identity=True,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=True,
        ),
        "error_without_visible_response": FinalDeliveryPolicy(
            emits_after_response=False,
            emits_cancelled_response=True,
            visible_response_event_source="none",
            response_identity_event_source="none",
            turn_completion_event_source="none",
            should_mark_handled=False,
            retryable=True,
            should_persist_response_identity=False,
            should_queue_thread_summary=False,
            should_register_interactive_follow_up=False,
            should_shield_late_failures=False,
        ),
    },
)


@dataclass(frozen=True)
class TurnDeliveryResolution:
    """Typed caller-facing projection of one canonical terminal state."""

    state: FinalDeliveryState
    visible_response_event_id: str | None
    response_identity_event_id: str | None
    turn_completion_event_id: str | None
    should_mark_handled: bool
    retryable: bool
    has_visible_output: bool

    @classmethod
    def from_outcome(cls, outcome: FinalDeliveryOutcome) -> TurnDeliveryResolution:
        """Project one canonical outcome into the caller-facing delivery view."""
        visible_response_event_id = outcome.visible_response_event_id
        return cls(
            state=outcome.state,
            visible_response_event_id=visible_response_event_id,
            response_identity_event_id=outcome.response_identity_event_id,
            turn_completion_event_id=outcome.turn_completion_event_id,
            should_mark_handled=outcome.should_mark_handled,
            retryable=outcome.retryable,
            has_visible_output=visible_response_event_id is not None,
        )


@dataclass(frozen=True)
class FinalDeliveryOutcome:
    """Canonical semantic terminal delivery outcome."""

    state: FinalDeliveryState
    terminal_status: TerminalStatus
    final_visible_event_id: str | None
    last_physical_stream_event_id: str | None
    final_visible_body: str | None
    delivery_kind: VisibleDeliveryKind | None = None
    failure_reason: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: Mapping[str, Any] = EMPTY_MAPPING
    option_map: Mapping[str, str] | None = None
    options_list: tuple[Mapping[str, str], ...] | None = None

    def __post_init__(self) -> None:
        """Freeze payloads and validate one canonical terminal state."""
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace))
        object.__setattr__(self, "extra_content", _freeze_mapping(self.extra_content))
        object.__setattr__(self, "option_map", _freeze_string_mapping(self.option_map))
        object.__setattr__(self, "options_list", _freeze_options_list(self.options_list))
        self._validate_state()

    def _validate_state(self) -> None:
        try:
            rule = LEGAL_FINAL_DELIVERY_STATES[self.state]
        except KeyError as error:
            msg = f"unknown final delivery state: {self.state}"
            raise ValueError(msg) from error

        if self.terminal_status != rule.terminal_status:
            msg = f"{self.state} requires terminal_status={rule.terminal_status!r}, got {self.terminal_status!r}"
            raise ValueError(msg)
        if rule.requires_final_visible_event and self.final_visible_event_id is None:
            msg = f"{self.state} requires final_visible_event_id"
            raise ValueError(msg)
        if not rule.allows_final_visible_event and self.final_visible_event_id is not None:
            msg = f"{self.state} forbids final_visible_event_id"
            raise ValueError(msg)
        if rule.requires_final_visible_body and self.final_visible_body is None:
            msg = f"{self.state} requires final_visible_body"
            raise ValueError(msg)
        if not rule.allows_final_visible_body and self.final_visible_body is not None:
            msg = f"{self.state} forbids final_visible_body"
            raise ValueError(msg)
        if self.final_visible_event_id is None and self.delivery_kind is not None:
            msg = f"{self.state} forbids delivery_kind without final_visible_event_id"
            raise ValueError(msg)
        if rule.requires_prior_visible_stream and self.last_physical_stream_event_id is None:
            msg = f"{self.state} requires last_physical_stream_event_id"
            raise ValueError(msg)
        if not rule.allows_prior_visible_stream and self.last_physical_stream_event_id is not None:
            msg = f"{self.state} forbids last_physical_stream_event_id"
            raise ValueError(msg)

    @property
    def policy(self) -> FinalDeliveryPolicy:
        """Return the single-source policy row for this state."""
        return FINAL_DELIVERY_POLICY[self.state]

    def _event_id_from(self, source: EventIdSource) -> str | None:
        if source == "none":
            return None
        if source == "final_visible_event":
            return self.final_visible_event_id
        return self.last_physical_stream_event_id

    @property
    def visible_response_event_id(self) -> str | None:
        """Return the event id that remains visible after terminal delivery settles."""
        return self._event_id_from(self.policy.visible_response_event_source)

    @property
    def response_identity_event_id(self) -> str | None:
        """Return the event id that should persist as the response identity."""
        return self._event_id_from(self.policy.response_identity_event_source)

    @property
    def turn_completion_event_id(self) -> str | None:
        """Return the event id that marks visible terminal completion."""
        return self._event_id_from(self.policy.turn_completion_event_source)

    @property
    def emits_after_response(self) -> bool:
        """Return whether this state fires the after-response hook."""
        return self.policy.emits_after_response

    @property
    def emits_cancelled_response(self) -> bool:
        """Return whether this state fires the terminal failure hook."""
        return self.policy.emits_cancelled_response

    @property
    def should_mark_handled(self) -> bool:
        """Return whether the outer caller should mark the turn handled."""
        return self.policy.should_mark_handled

    @property
    def retryable(self) -> bool:
        """Return whether this state is eligible for retry handling."""
        return self.policy.retryable

    @property
    def should_persist_response_identity(self) -> bool:
        """Return whether response linkage should persist for this state."""
        return self.policy.should_persist_response_identity

    @property
    def should_queue_thread_summary(self) -> bool:
        """Return whether thread-summary follow-up should queue for this state."""
        return self.policy.should_queue_thread_summary

    @property
    def should_register_interactive_follow_up(self) -> bool:
        """Return whether interactive follow-up should register for this state."""
        return self.policy.should_register_interactive_follow_up

    @property
    def should_shield_late_failures(self) -> bool:
        """Return whether late post-effect failures should be downgraded."""
        return self.policy.should_shield_late_failures

    @property
    def has_final_visible_delivery(self) -> bool:
        """Return whether the final terminal artifact is a successful visible reply."""
        return self.state == "final_visible_delivery"

    @property
    def has_any_visible_response(self) -> bool:
        """Return whether any visible response artifact remains in the room."""
        return self.visible_response_event_id is not None

    @property
    def logical_response_event_id(self) -> str | None:
        """Return the compatibility alias for response identity."""
        return self.response_identity_event_id

    @property
    def event_id(self) -> str | None:
        """Return the compatibility alias for the visible event id."""
        return self.visible_response_event_id

    @property
    def response_text(self) -> str:
        """Return the compatibility alias for the final visible body."""
        return self.final_visible_body or ""

    @property
    def suppressed(self) -> bool:
        """Return whether this outcome ended in one suppression state."""
        return self.state in {
            "kept_prior_visible_response_after_suppression",
            "suppressed_without_visible_response",
            "suppressed_redacted",
            "suppression_cleanup_failed",
        }

    @classmethod
    def final_visible_delivery(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str,
        last_physical_stream_event_id: str | None = None,
        delivery_kind: VisibleDeliveryKind | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the completed state where final visible delivery succeeded."""
        return cls(
            state="final_visible_delivery",
            terminal_status="completed",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            delivery_kind=delivery_kind,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def keep_prior_visible_stream_after_completed_terminal_failure(
        cls,
        *,
        last_physical_stream_event_id: str,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the completed state where a prior visible stream survives terminal failure."""
        return cls(
            state="kept_prior_visible_stream_after_completed_terminal_failure",
            terminal_status="completed",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def keep_prior_visible_stream_after_cancel(
        cls,
        *,
        last_physical_stream_event_id: str,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the cancelled state where a prior visible stream survives."""
        return cls(
            state="kept_prior_visible_stream_after_cancel",
            terminal_status="cancelled",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def keep_prior_visible_stream_after_error(
        cls,
        *,
        last_physical_stream_event_id: str,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where a prior visible stream survives."""
        return cls(
            state="kept_prior_visible_stream_after_error",
            terminal_status="error",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def kept_prior_visible_response_after_suppression(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where a prior visible response remains unchanged."""
        return cls(
            state="kept_prior_visible_response_after_suppression",
            terminal_status="completed",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=None,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def kept_prior_visible_response_after_error(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where a prior visible response remains unchanged."""
        return cls(
            state="kept_prior_visible_response_after_error",
            terminal_status="error",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=None,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def cancelled_with_visible_response(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str | None = None,
        last_physical_stream_event_id: str | None = None,
        delivery_kind: VisibleDeliveryKind | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the cancelled state where an already-visible response remains."""
        return cls(
            state="cancelled_with_visible_response",
            terminal_status="cancelled",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            delivery_kind=delivery_kind,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def cancelled_with_visible_note(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str,
        last_physical_stream_event_id: str | None = None,
        delivery_kind: VisibleDeliveryKind | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the cancelled state where a final visible cancellation note landed."""
        return cls(
            state="cancelled_with_visible_note",
            terminal_status="cancelled",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            delivery_kind=delivery_kind,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def cancelled_without_visible_response(
        cls,
        *,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the cancelled state where no visible response artifact remains."""
        return cls(
            state="cancelled_without_visible_response",
            terminal_status="cancelled",
            final_visible_event_id=None,
            last_physical_stream_event_id=None,
            final_visible_body=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def suppressed_without_visible_response(
        cls,
        *,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where nothing ever became visible."""
        return cls(
            state="suppressed_without_visible_response",
            terminal_status="completed",
            final_visible_event_id=None,
            last_physical_stream_event_id=None,
            final_visible_body=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def suppressed_redacted(
        cls,
        *,
        last_physical_stream_event_id: str,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where visible output was cleaned up successfully."""
        return cls(
            state="suppressed_redacted",
            terminal_status="completed",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def suppression_cleanup_failed(
        cls,
        *,
        last_physical_stream_event_id: str,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where cleanup failed after visibility leaked."""
        return cls(
            state="suppression_cleanup_failed",
            terminal_status="completed",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def error_with_visible_response(
        cls,
        *,
        final_visible_event_id: str,
        final_visible_body: str,
        last_physical_stream_event_id: str | None = None,
        delivery_kind: VisibleDeliveryKind | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where a final visible error artifact landed."""
        return cls(
            state="error_with_visible_response",
            terminal_status="error",
            final_visible_event_id=final_visible_event_id,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            delivery_kind=delivery_kind,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
            option_map=option_map,
            options_list=tuple(options_list) if options_list is not None else None,
        )

    @classmethod
    def error_without_visible_response(
        cls,
        *,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where no visible response artifact exists."""
        return cls(
            state="error_without_visible_response",
            terminal_status="error",
            final_visible_event_id=None,
            last_physical_stream_event_id=None,
            final_visible_body=None,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )
