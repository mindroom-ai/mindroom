"""Canonical terminal delivery facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.cancellation import cancel_source_from_failure_reason

if TYPE_CHECKING:
    from mindroom.cancellation import CancelSource
    from mindroom.interactive import InteractiveMetadata
    from mindroom.tool_system.events import ToolTraceEntry

_TerminalStatus = Literal["completed", "cancelled", "error"]
VisibleBodyState = Literal["none", "placeholder_only", "visible_body"]
_VisibleDeliveryKind = Literal["sent", "edited"]


@dataclass(frozen=True)
class StreamTransportOutcome:  # noqa: D101
    last_physical_stream_event_id: str | None
    terminal_status: _TerminalStatus
    rendered_body: str | None
    visible_body_state: VisibleBodyState
    terminal_update_committed: bool = False
    canonical_final_body_candidate: str | None = None
    failure_reason: str | None = None
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

    @property
    def resolved_cancel_source(self) -> CancelSource | None:
        """Resolve cancellation provenance from the persisted transport failure reason."""
        if self.terminal_status != "cancelled":
            return None
        return cancel_source_from_failure_reason(self.failure_reason)


@dataclass(frozen=True)
class FinalDeliveryOutcome:  # noqa: D101
    terminal_status: _TerminalStatus
    event_id: str | None
    is_visible_response: bool = False
    final_visible_body: str | None = None
    delivery_kind: _VisibleDeliveryKind | None = None
    cancel_source: Literal["user_stop", "sync_restart", "interrupted"] | None = None
    failure_reason: str | None = None
    suppressed: bool = False
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: dict[str, Any] | None = None
    interactive_metadata: InteractiveMetadata | None = None

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "tool_trace", tuple(self.tool_trace or ()))
        object.__setattr__(self, "extra_content", dict(self.extra_content or {}))

    @property
    def final_visible_event_id(self) -> str | None:  # noqa: D102
        return self.event_id if self.is_visible_response else None

    @property
    def resolved_cancel_source(self) -> CancelSource | None:
        """Resolve cancellation provenance: explicit provenance wins, else the persisted failure reason."""
        if self.cancel_source is not None:
            return self.cancel_source
        if self.terminal_status == "cancelled":
            return cancel_source_from_failure_reason(self.failure_reason)
        return None

    @property
    def mark_handled(self) -> bool:  # noqa: D102
        return self.event_id is not None and self.is_visible_response and not self.suppressed

    @property
    def delivered_substantive_content(self) -> bool:
        """Return whether this outcome proves that nonblank response text reached Matrix."""
        return (
            self.terminal_status == "completed"
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

    @classmethod
    def cancelled_for_empty_prompt(cls) -> FinalDeliveryOutcome:
        """Return the canonical empty-prompt terminal outcome."""
        return cls(
            terminal_status="cancelled",
            event_id=None,
            failure_reason="empty_prompt",
        )
