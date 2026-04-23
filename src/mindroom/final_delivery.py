"""Canonical terminal delivery facts."""

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


def _copy_dict(value: dict[str, str] | dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(value) if value else None


def _copy_options(value: tuple[dict[str, str], ...] | list[dict[str, str]] | None) -> tuple[dict[str, str], ...] | None:
    return tuple(dict(item) for item in value) if value is not None else None


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
        object.__setattr__(self, "options_list", _copy_options(self.options_list))
        if self.terminal_result == "not_attempted" and self.terminal_operation != "none":
            raise ValueError("terminal_operation must be 'none' when terminal_result is 'not_attempted'")  # noqa: EM101, TRY003
        if self.terminal_result != "not_attempted" and self.terminal_operation == "none":
            raise ValueError("terminal_operation cannot be 'none' when a terminal result exists")  # noqa: EM101, TRY003
        if self.visible_body_state == "none" and self.rendered_body is not None:
            raise ValueError("visible_body_state 'none' cannot carry a rendered_body")  # noqa: EM101, TRY003
        if self.visible_body_state != "none" and self.rendered_body is None:
            raise ValueError("visible_body_state requires a rendered_body")  # noqa: EM101, TRY003

    @property
    def has_any_physical_stream_event(self) -> bool:  # noqa: D102
        return self.last_physical_stream_event_id is not None

    @property
    def has_rendered_visible_body(self) -> bool:  # noqa: D102
        return self.visible_body_state == "visible_body"


@dataclass(frozen=True)
class FinalDeliveryOutcome:  # noqa: D101
    terminal_status: TerminalStatus
    final_visible_event_id: str | None
    visible_response_event_id: str | None = None
    response_identity_event_id: str | None = None
    turn_completion_event_id: str | None = None
    last_physical_stream_event_id: str | None = None
    final_visible_body: str | None = None
    canonical_final_body_candidate: str | None = None
    delivery_kind: VisibleDeliveryKind | None = None
    failure_reason: str | None = None
    mark_handled: bool = False
    retryable: bool = False
    suppressed: bool = False
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: dict[str, Any] | None = None
    option_map: dict[str, str] | None = None
    options_list: tuple[dict[str, str], ...] | None = None

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace or ()))
        object.__setattr__(self, "extra_content", dict(self.extra_content or {}))
        object.__setattr__(self, "option_map", _copy_dict(self.option_map))
        object.__setattr__(self, "options_list", _copy_options(self.options_list))
        if self.delivery_kind is not None and self.final_visible_event_id is None:
            raise ValueError("delivery_kind requires final_visible_event_id")  # noqa: EM101, TRY003
        if self.response_identity_event_id is not None and self.visible_response_event_id is None:
            raise ValueError("response_identity_event_id requires visible_response_event_id")  # noqa: EM101, TRY003

    @property
    def event_id(self) -> str | None:  # noqa: D102
        return self.visible_response_event_id

    @property
    def response_text(self) -> str:  # noqa: D102
        return self.final_visible_body or ""
