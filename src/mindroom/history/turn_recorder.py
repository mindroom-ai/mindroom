"""Live top-level turn recording for canonical interrupted replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.history.interrupted_replay import InterruptedReplaySnapshot, build_interrupted_replay_snapshot

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class TurnRecorder:
    """Accumulate trusted runtime facts for one top-level turn."""

    user_message: str
    run_metadata: dict[str, Any] | None = None
    run_id: str | None = None
    response_event_id: str | None = None
    assistant_text: str = ""
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    interrupted_tools: list[ToolTraceEntry] = field(default_factory=list)
    outcome: str = "pending"
    interrupted_persisted: bool = False

    def set_run_metadata(self, metadata: dict[str, Any] | None) -> None:
        """Replace the current Matrix run metadata snapshot."""
        self.run_metadata = dict(metadata) if metadata is not None else None

    def set_run_id(self, run_id: str | None) -> None:
        """Replace the current top-level Agno run identifier."""
        self.run_id = run_id or None

    def set_response_event_id(self, response_event_id: str | None) -> None:
        """Replace the current visible Matrix response event identifier."""
        self.response_event_id = response_event_id or None

    def set_assistant_text(self, text: str) -> None:
        """Replace the canonical assistant text observed so far."""
        self.assistant_text = text

    def set_completed_tools(self, tools: list[ToolTraceEntry]) -> None:
        """Replace the completed tool list."""
        self.completed_tools = list(tools)

    def set_interrupted_tools(self, tools: list[ToolTraceEntry]) -> None:
        """Replace the in-flight interrupted tool list."""
        self.interrupted_tools = list(tools)

    def record_completed(
        self,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record one completed top-level turn."""
        self.set_run_metadata(dict(run_metadata) if run_metadata is not None else None)
        self.set_assistant_text(assistant_text)
        self.set_completed_tools(list(completed_tools))
        self.set_interrupted_tools([])
        self.mark_completed()

    def record_interrupted(
        self,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record one interrupted top-level turn."""
        self.set_run_metadata(dict(run_metadata) if run_metadata is not None else None)
        self.set_assistant_text(assistant_text)
        self.set_completed_tools(list(completed_tools))
        self.set_interrupted_tools(list(interrupted_tools))
        self.mark_interrupted()

    def mark_completed(self) -> None:
        """Record successful completion."""
        self.outcome = "completed"

    def mark_interrupted(self) -> None:
        """Record interruption."""
        self.outcome = "interrupted"

    def interrupted_snapshot(self) -> InterruptedReplaySnapshot:
        """Build one canonical interrupted snapshot from the recorded facts."""
        return build_interrupted_replay_snapshot(
            user_message=self.user_message,
            partial_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
            run_metadata=self.run_metadata,
            response_event_id=self.response_event_id,
        )

    def claim_interrupted_persistence(self) -> bool:
        """Return whether one interrupted turn should be persisted now."""
        if self.outcome != "interrupted" or self.interrupted_persisted:
            return False
        self.interrupted_persisted = True
        return True
