"""Terminal response outcome helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mindroom.final_delivery import StreamTransportOutcome, VisibleBodyState

TerminalFailureStatus = Literal["cancelled", "error"]


@dataclass(frozen=True)
class PendingVisibleResponse:
    """Visible response candidates still pending at terminal cleanup time."""

    tracked_event_id: str | None
    run_message_id: str | None
    existing_event_id: str | None
    existing_event_is_placeholder: bool

    @property
    def terminal_event_id(self) -> str | None:
        """Return the Matrix event that terminal cleanup should finalize."""
        if self.tracked_event_id is not None:
            return self.tracked_event_id
        if self.run_message_id is not None:
            return self.run_message_id
        if self.existing_event_id is not None and self.existing_event_is_placeholder:
            return self.existing_event_id
        return None

    def is_placeholder_only(self, event_id: str | None) -> bool:
        """Return whether the terminal event only contains the progress placeholder."""
        if event_id is None:
            return False
        if self.run_message_id is not None and event_id == self.run_message_id:
            return True
        return (
            self.existing_event_id is not None
            and self.existing_event_is_placeholder
            and event_id == self.existing_event_id
        )


def build_terminal_stream_transport_outcome(
    pending: PendingVisibleResponse,
    *,
    terminal_status: TerminalFailureStatus,
    failure_reason: str,
    placeholder_body: str,
) -> StreamTransportOutcome:
    """Build the canonical stream outcome for a terminal failure or cancellation."""
    event_id = pending.terminal_event_id
    placeholder_only = pending.is_placeholder_only(event_id)
    rendered_body = placeholder_body if placeholder_only else None
    visible_body_state: VisibleBodyState = "placeholder_only" if placeholder_only else "none"
    return StreamTransportOutcome(
        last_physical_stream_event_id=event_id,
        terminal_status=terminal_status,
        rendered_body=rendered_body,
        visible_body_state=visible_body_state,
        failure_reason=failure_reason,
    )


__all__ = [
    "PendingVisibleResponse",
    "TerminalFailureStatus",
    "build_terminal_stream_transport_outcome",
]
