"""Canonical terminal delivery outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

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
    "error_without_visible_response",
]


@dataclass(frozen=True)
class StreamTransportOutcome:
    """Transport facts emitted by streamed delivery finalization."""

    last_physical_stream_event_id: str | None
    terminal_operation: StreamTerminalOperation
    terminal_result: StreamTerminalResult
    terminal_status: TerminalStatus
    rendered_body: str | None
    visible_body_state: VisibleBodyState
    canonical_final_body_candidate: str | None = None
    failure_reason: str | None = None
    option_map: dict[str, str] | None = None
    options_list: tuple[dict[str, str], ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "option_map", dict(self.option_map) if self.option_map else None)
        object.__setattr__(
            self,
            "options_list",
            tuple(dict(item) for item in self.options_list) if self.options_list is not None else None,
        )
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
        return self.last_physical_stream_event_id is not None

    @property
    def has_rendered_visible_body(self) -> bool:
        return self.visible_body_state == "visible_body"


@dataclass(frozen=True)
class FinalDeliveryPolicy:
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


def _policy(
    emits_after_response: bool,
    emits_cancelled_response: bool,
    visible_response_event_source: EventIdSource,
    response_identity_event_source: EventIdSource,
    turn_completion_event_source: EventIdSource,
    should_mark_handled: bool,
    retryable: bool,
    should_persist_response_identity: bool,
    should_queue_thread_summary: bool,
    should_register_interactive_follow_up: bool,
    should_shield_late_failures: bool,
) -> FinalDeliveryPolicy:
    return FinalDeliveryPolicy(
        emits_after_response=emits_after_response,
        emits_cancelled_response=emits_cancelled_response,
        visible_response_event_source=visible_response_event_source,
        response_identity_event_source=response_identity_event_source,
        turn_completion_event_source=turn_completion_event_source,
        should_mark_handled=should_mark_handled,
        retryable=retryable,
        should_persist_response_identity=should_persist_response_identity,
        should_queue_thread_summary=should_queue_thread_summary,
        should_register_interactive_follow_up=should_register_interactive_follow_up,
        should_shield_late_failures=should_shield_late_failures,
    )


FINAL_DELIVERY_POLICY: dict[FinalDeliveryState, FinalDeliveryPolicy] = {
    "final_visible_delivery": _policy(
        True,
        False,
        "final_visible_event",
        "final_visible_event",
        "final_visible_event",
        True,
        False,
        True,
        True,
        True,
        True,
    ),
    "kept_prior_visible_stream_after_completed_terminal_failure": _policy(
        False,
        True,
        "last_physical_stream_event",
        "last_physical_stream_event",
        "last_physical_stream_event",
        True,
        False,
        True,
        True,
        True,
        True,
    ),
    "kept_prior_visible_stream_after_cancel": _policy(
        False,
        True,
        "last_physical_stream_event",
        "none",
        "last_physical_stream_event",
        False,
        True,
        False,
        False,
        False,
        True,
    ),
    "kept_prior_visible_stream_after_error": _policy(
        False,
        True,
        "last_physical_stream_event",
        "last_physical_stream_event",
        "last_physical_stream_event",
        True,
        False,
        True,
        False,
        False,
        True,
    ),
    "kept_prior_visible_response_after_suppression": _policy(
        False, True, "final_visible_event", "none", "none", False, True, False, False, False, False
    ),
    "kept_prior_visible_response_after_error": _policy(
        False, True, "final_visible_event", "none", "none", False, True, False, False, False, False
    ),
    "cancelled_with_visible_response": _policy(
        False, True, "final_visible_event", "none", "final_visible_event", False, True, False, False, False, True
    ),
    "cancelled_with_visible_note": _policy(
        False, True, "final_visible_event", "none", "final_visible_event", False, True, False, False, False, True
    ),
    "cancelled_without_visible_response": _policy(
        False, True, "none", "none", "none", False, True, False, False, False, False
    ),
    "suppressed_without_visible_response": _policy(
        False, True, "none", "none", "none", True, False, False, False, False, False
    ),
    "suppressed_redacted": _policy(False, True, "none", "none", "none", True, False, False, False, False, False),
    "suppression_cleanup_failed": _policy(
        False,
        True,
        "last_physical_stream_event",
        "none",
        "last_physical_stream_event",
        True,
        False,
        False,
        False,
        False,
        True,
    ),
    "error_without_visible_response": _policy(
        False, True, "none", "none", "none", False, True, False, False, False, False
    ),
}


@dataclass(frozen=True)
class FinalDeliveryOutcome:
    """Canonical semantic terminal delivery outcome."""

    state: FinalDeliveryState
    terminal_status: TerminalStatus
    final_visible_event_id: str | None
    last_physical_stream_event_id: str | None
    final_visible_body: str | None = None
    delivery_kind: VisibleDeliveryKind | None = None
    failure_reason: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: dict[str, Any] | None = None
    option_map: dict[str, str] | None = None
    options_list: tuple[dict[str, str], ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace))
        object.__setattr__(self, "extra_content", dict(self.extra_content or {}))
        object.__setattr__(self, "option_map", dict(self.option_map) if self.option_map else None)
        object.__setattr__(
            self,
            "options_list",
            tuple(dict(item) for item in self.options_list) if self.options_list is not None else None,
        )
        if self.final_visible_event_id is None and self.delivery_kind is not None:
            msg = "delivery_kind requires final_visible_event_id"
            raise ValueError(msg)

    @property
    def policy(self) -> FinalDeliveryPolicy:
        return FINAL_DELIVERY_POLICY[self.state]

    def _event_id_from(self, source: EventIdSource) -> str | None:
        if source == "none":
            return None
        if source == "final_visible_event":
            return self.final_visible_event_id
        return self.last_physical_stream_event_id

    @property
    def visible_response_event_id(self) -> str | None:
        return self._event_id_from(self.policy.visible_response_event_source)

    @property
    def response_identity_event_id(self) -> str | None:
        return self._event_id_from(self.policy.response_identity_event_source)

    @property
    def turn_completion_event_id(self) -> str | None:
        return self._event_id_from(self.policy.turn_completion_event_source)

    @property
    def emits_after_response(self) -> bool:
        return self.policy.emits_after_response

    @property
    def emits_cancelled_response(self) -> bool:
        return self.policy.emits_cancelled_response

    @property
    def should_mark_handled(self) -> bool:
        return self.policy.should_mark_handled

    @property
    def retryable(self) -> bool:
        return self.policy.retryable

    @property
    def should_persist_response_identity(self) -> bool:
        return self.policy.should_persist_response_identity

    @property
    def should_queue_thread_summary(self) -> bool:
        return self.policy.should_queue_thread_summary

    @property
    def should_register_interactive_follow_up(self) -> bool:
        return self.policy.should_register_interactive_follow_up

    @property
    def should_shield_late_failures(self) -> bool:
        return self.policy.should_shield_late_failures

    @property
    def has_final_visible_delivery(self) -> bool:
        return self.state == "final_visible_delivery"

    @property
    def has_any_visible_response(self) -> bool:
        return self.visible_response_event_id is not None

    @property
    def event_id(self) -> str | None:
        return self.visible_response_event_id

    @property
    def response_text(self) -> str:
        return self.final_visible_body or ""

    @property
    def suppressed(self) -> bool:
        return self.state in {
            "kept_prior_visible_response_after_suppression",
            "suppressed_without_visible_response",
            "suppressed_redacted",
            "suppression_cleanup_failed",
        }
