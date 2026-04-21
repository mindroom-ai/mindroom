"""Canonical terminal delivery state shared across streamed and final response paths."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
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
FinalDeliveryState = Literal[
    "final_visible_delivery",
    "kept_prior_visible_stream_after_completed_terminal_failure",
    "kept_prior_visible_stream_after_cancel",
    "kept_prior_visible_stream_after_error",
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
            MappingProxyType({key: _freeze_value(nested_value) for key, nested_value in value.items()}),
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

    def __post_init__(self) -> None:
        """Validate transport invariants for one terminal streaming snapshot."""
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
        """Return whether streaming ever produced a physically visible event."""
        return self.last_physical_stream_event_id is not None

    @property
    def has_rendered_visible_body(self) -> bool:
        """Return whether the rendered body contains real visible output."""
        return self.visible_body_state == "visible_body"

    @property
    def event_id(self) -> str | None:
        """Return the legacy event-id view of the last physical stream event."""
        return self.last_physical_stream_event_id

    @property
    def accumulated_text(self) -> str:
        """Return the legacy text view backed by the rendered terminal body."""
        return self.rendered_body or ""

    def __iter__(self) -> Iterator[str | None]:
        """Support legacy tuple unpacking during the transport-boundary migration."""
        yield self.last_physical_stream_event_id
        yield self.rendered_body or ""


@dataclass(frozen=True)
class _StateRule:
    terminal_status: TerminalStatus
    requires_final_visible_event: bool = False
    requires_final_visible_body: bool = False
    allows_final_visible_event: bool = True
    allows_final_visible_body: bool = True
    requires_prior_visible_stream: bool = False
    allows_prior_visible_stream: bool = True


LEGAL_FINAL_DELIVERY_STATES: Mapping[FinalDeliveryState, _StateRule] = MappingProxyType(
    {
        "final_visible_delivery": _StateRule(
            terminal_status="completed",
            requires_final_visible_event=True,
            requires_final_visible_body=True,
        ),
        "kept_prior_visible_stream_after_completed_terminal_failure": _StateRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "kept_prior_visible_stream_after_cancel": _StateRule(
            terminal_status="cancelled",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "kept_prior_visible_stream_after_error": _StateRule(
            terminal_status="error",
            allows_final_visible_event=False,
            requires_prior_visible_stream=True,
        ),
        "cancelled_with_visible_response": _StateRule(
            terminal_status="cancelled",
            requires_final_visible_event=True,
        ),
        "cancelled_with_visible_note": _StateRule(
            terminal_status="cancelled",
            requires_final_visible_event=True,
            requires_final_visible_body=True,
        ),
        "cancelled_without_visible_response": _StateRule(
            terminal_status="cancelled",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
        "suppressed_without_visible_response": _StateRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
        "suppressed_redacted": _StateRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            requires_prior_visible_stream=True,
        ),
        "suppression_cleanup_failed": _StateRule(
            terminal_status="completed",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            requires_prior_visible_stream=True,
        ),
        "error_with_visible_response": _StateRule(
            terminal_status="error",
            requires_final_visible_event=True,
        ),
        "error_without_visible_response": _StateRule(
            terminal_status="error",
            allows_final_visible_event=False,
            allows_final_visible_body=False,
            allows_prior_visible_stream=False,
        ),
    },
)


@dataclass(frozen=True)
class FinalDeliveryOutcome:
    """Canonical terminal delivery outcome shared across downstream layers."""

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
        """Freeze mutable payloads and validate the canonical state mapping."""
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
    def has_final_visible_delivery(self) -> bool:
        """Return whether terminal delivery landed as a visible response event."""
        return self.state == "final_visible_delivery"

    @property
    def has_any_visible_response(self) -> bool:
        """Return whether any visible response exists, even if only a prior stream survived."""
        return self.visible_response_event_id is not None

    @property
    def visible_response_event_id(self) -> str | None:
        """Return the event id that is still visibly present after terminal delivery settles."""
        if self.state in {
            "final_visible_delivery",
            "cancelled_with_visible_response",
            "cancelled_with_visible_note",
            "error_with_visible_response",
        }:
            return self.final_visible_event_id
        if self.state in {
            "kept_prior_visible_stream_after_completed_terminal_failure",
            "kept_prior_visible_stream_after_cancel",
            "kept_prior_visible_stream_after_error",
            "suppression_cleanup_failed",
        }:
            return self.last_physical_stream_event_id
        return None

    @property
    def response_identity_event_id(self) -> str | None:
        """Return the event id that should remain associated with the turn downstream."""
        if self.state in {
            "final_visible_delivery",
            "error_with_visible_response",
        }:
            return self.final_visible_event_id
        if self.state in {
            "kept_prior_visible_stream_after_completed_terminal_failure",
            "kept_prior_visible_stream_after_error",
        }:
            return self.last_physical_stream_event_id
        return None

    @property
    def logical_response_event_id(self) -> str | None:
        """Compatibility alias for the downstream response-identity event id."""
        return self.response_identity_event_id

    @property
    def event_id(self) -> str | None:
        """Return the still-visible event id for compatibility callers."""
        return self.visible_response_event_id

    @property
    def response_text(self) -> str:
        """Return the final visible body for compatibility callers."""
        return self.final_visible_body or ""

    @property
    def suppressed(self) -> bool:
        """Return whether this terminal outcome ended in one suppression state."""
        return self.state in {
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
        last_physical_stream_event_id: str | None,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the completed state where terminal delivery failed after a visible stream."""
        return cls(
            state="kept_prior_visible_stream_after_completed_terminal_failure",
            terminal_status="completed",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def keep_prior_visible_stream_after_cancel(
        cls,
        *,
        last_physical_stream_event_id: str | None,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the cancelled state where a prior visible stream remains visible."""
        return cls(
            state="kept_prior_visible_stream_after_cancel",
            terminal_status="cancelled",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
            final_visible_body=final_visible_body,
            failure_reason=failure_reason,
            tool_trace=tuple(tool_trace),
            extra_content=extra_content or EMPTY_MAPPING,
        )

    @classmethod
    def keep_prior_visible_stream_after_error(
        cls,
        *,
        last_physical_stream_event_id: str | None,
        final_visible_body: str | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where a prior visible stream remains visible."""
        return cls(
            state="kept_prior_visible_stream_after_error",
            terminal_status="error",
            final_visible_event_id=None,
            last_physical_stream_event_id=last_physical_stream_event_id,
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
        """Build the cancelled state where a prior visible response remains on screen."""
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
        """Build the cancelled state where nothing user-visible was delivered."""
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
        """Build the suppressed state where no visible response existed to redact."""
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
        last_physical_stream_event_id: str | None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where a visible stream was redacted successfully."""
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
        last_physical_stream_event_id: str | None,
        failure_reason: str | None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the suppressed state where redaction cleanup failed after visibility."""
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
        final_visible_body: str | None = None,
        last_physical_stream_event_id: str | None = None,
        delivery_kind: VisibleDeliveryKind | None = None,
        failure_reason: str | None = None,
        tool_trace: Sequence[ToolTraceEntry] = (),
        extra_content: Mapping[str, Any] | None = None,
        option_map: Mapping[str, str] | None = None,
        options_list: Sequence[Mapping[str, str]] | None = None,
    ) -> FinalDeliveryOutcome:
        """Build the error state where a final visible error response landed."""
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
        """Build the error state where nothing visible was delivered."""
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
