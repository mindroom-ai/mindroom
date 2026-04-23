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

_AFTER_RESPONSE = 1
_CANCELLED_RESPONSE = 2
_MARK_HANDLED = 4
_RETRYABLE = 8
_PERSIST_RESPONSE_IDENTITY = 16
_QUEUE_THREAD_SUMMARY = 32
_REGISTER_INTERACTIVE_FOLLOW_UP = 64
_SHIELD_LATE_FAILURES = 128

_VISIBLE_EVENT = 0
_RESPONSE_EVENT = 1
_TURN_COMPLETION_EVENT = 2
_FLAGS = 3

FinalDeliveryPolicy = tuple[EventIdSource, EventIdSource, EventIdSource, int]


FINAL_DELIVERY_POLICY: dict[FinalDeliveryState, FinalDeliveryPolicy] = {
    "final_visible_delivery": (
        "final_visible_event",
        "final_visible_event",
        "final_visible_event",
        _AFTER_RESPONSE
        | _MARK_HANDLED
        | _PERSIST_RESPONSE_IDENTITY
        | _QUEUE_THREAD_SUMMARY
        | _REGISTER_INTERACTIVE_FOLLOW_UP
        | _SHIELD_LATE_FAILURES,
    ),
    "kept_prior_visible_stream_after_completed_terminal_failure": (
        "last_physical_stream_event",
        "last_physical_stream_event",
        "last_physical_stream_event",
        _CANCELLED_RESPONSE
        | _MARK_HANDLED
        | _PERSIST_RESPONSE_IDENTITY
        | _QUEUE_THREAD_SUMMARY
        | _REGISTER_INTERACTIVE_FOLLOW_UP
        | _SHIELD_LATE_FAILURES,
    ),
    "kept_prior_visible_stream_after_cancel": (
        "last_physical_stream_event",
        "none",
        "last_physical_stream_event",
        _CANCELLED_RESPONSE | _RETRYABLE | _SHIELD_LATE_FAILURES,
    ),
    "kept_prior_visible_stream_after_error": (
        "last_physical_stream_event",
        "last_physical_stream_event",
        "last_physical_stream_event",
        _CANCELLED_RESPONSE | _MARK_HANDLED | _PERSIST_RESPONSE_IDENTITY | _SHIELD_LATE_FAILURES,
    ),
    "kept_prior_visible_response_after_suppression": (
        "final_visible_event",
        "none",
        "none",
        _CANCELLED_RESPONSE | _RETRYABLE,
    ),
    "kept_prior_visible_response_after_error": (
        "final_visible_event",
        "none",
        "none",
        _CANCELLED_RESPONSE | _RETRYABLE,
    ),
    "cancelled_with_visible_response": (
        "final_visible_event",
        "none",
        "final_visible_event",
        _CANCELLED_RESPONSE | _RETRYABLE | _SHIELD_LATE_FAILURES,
    ),
    "cancelled_with_visible_note": (
        "final_visible_event",
        "none",
        "final_visible_event",
        _CANCELLED_RESPONSE | _RETRYABLE | _SHIELD_LATE_FAILURES,
    ),
    "cancelled_without_visible_response": ("none", "none", "none", _CANCELLED_RESPONSE | _RETRYABLE),
    "suppressed_without_visible_response": ("none", "none", "none", _CANCELLED_RESPONSE | _MARK_HANDLED),
    "suppressed_redacted": ("none", "none", "none", _CANCELLED_RESPONSE | _MARK_HANDLED),
    "suppression_cleanup_failed": (
        "last_physical_stream_event",
        "none",
        "last_physical_stream_event",
        _CANCELLED_RESPONSE | _MARK_HANDLED | _SHIELD_LATE_FAILURES,
    ),
    "error_without_visible_response": ("none", "none", "none", _CANCELLED_RESPONSE | _RETRYABLE),
}


def _copy_dict(value: dict[str, str] | dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(value) if value else None


def _copy_options_list(
    options_list: tuple[dict[str, str], ...] | list[dict[str, str]] | None,
) -> tuple[dict[str, str], ...] | None:
    return tuple(dict(item) for item in options_list) if options_list is not None else None


@dataclass(frozen=True)
class StreamTransportOutcome:  # noqa: D101
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

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "option_map", _copy_dict(self.option_map))
        object.__setattr__(self, "options_list", _copy_options_list(self.options_list))
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
    def has_any_physical_stream_event(self) -> bool:  # noqa: D102
        return self.last_physical_stream_event_id is not None

    @property
    def has_rendered_visible_body(self) -> bool:  # noqa: D102
        return self.visible_body_state == "visible_body"


@dataclass(frozen=True)
class FinalDeliveryOutcome:  # noqa: D101
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

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace))
        object.__setattr__(self, "extra_content", dict(self.extra_content or {}))
        object.__setattr__(self, "option_map", _copy_dict(self.option_map))
        object.__setattr__(self, "options_list", _copy_options_list(self.options_list))
        if self.final_visible_event_id is None and self.delivery_kind is not None:
            msg = "delivery_kind requires final_visible_event_id"
            raise ValueError(msg)

    def _policy_value(self, index: int) -> EventIdSource | int:
        return FINAL_DELIVERY_POLICY[self.state][index]

    def _event_id_from(self, index: int) -> str | None:
        source = self._policy_value(index)
        if source == "final_visible_event":
            return self.final_visible_event_id
        if source == "last_physical_stream_event":
            return self.last_physical_stream_event_id
        return None

    def _flags(self) -> int:
        return FINAL_DELIVERY_POLICY[self.state][_FLAGS]

    def _has_flag(self, flag: int) -> bool:
        return bool(self._flags() & flag)

    @property
    def visible_response_event_id(self) -> str | None:  # noqa: D102
        return self._event_id_from(_VISIBLE_EVENT)

    @property
    def response_identity_event_id(self) -> str | None:  # noqa: D102
        return self._event_id_from(_RESPONSE_EVENT)

    @property
    def turn_completion_event_id(self) -> str | None:  # noqa: D102
        return self._event_id_from(_TURN_COMPLETION_EVENT)

    @property
    def emits_after_response(self) -> bool:  # noqa: D102
        return self._has_flag(_AFTER_RESPONSE)

    @property
    def emits_cancelled_response(self) -> bool:  # noqa: D102
        return self._has_flag(_CANCELLED_RESPONSE)

    @property
    def should_mark_handled(self) -> bool:  # noqa: D102
        return self._has_flag(_MARK_HANDLED)

    @property
    def retryable(self) -> bool:  # noqa: D102
        return self._has_flag(_RETRYABLE)

    @property
    def should_persist_response_identity(self) -> bool:  # noqa: D102
        return self._has_flag(_PERSIST_RESPONSE_IDENTITY)

    @property
    def should_queue_thread_summary(self) -> bool:  # noqa: D102
        return self._has_flag(_QUEUE_THREAD_SUMMARY)

    @property
    def should_register_interactive_follow_up(self) -> bool:  # noqa: D102
        return self._has_flag(_REGISTER_INTERACTIVE_FOLLOW_UP)

    @property
    def should_shield_late_failures(self) -> bool:  # noqa: D102
        return self._has_flag(_SHIELD_LATE_FAILURES)

    @property
    def has_final_visible_delivery(self) -> bool:  # noqa: D102
        return self.state == "final_visible_delivery"

    @property
    def has_any_visible_response(self) -> bool:  # noqa: D102
        return self.visible_response_event_id is not None

    @property
    def event_id(self) -> str | None:  # noqa: D102
        return self.visible_response_event_id

    @property
    def response_text(self) -> str:  # noqa: D102
        return self.final_visible_body or ""

    @property
    def suppressed(self) -> bool:  # noqa: D102
        return self.state in {
            "kept_prior_visible_response_after_suppression",
            "suppressed_without_visible_response",
            "suppressed_redacted",
            "suppression_cleanup_failed",
        }
