"""Live top-level turn recording for canonical interrupted replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.history.interrupted_replay import InterruptedReplaySnapshot, build_interrupted_replay_snapshot

if TYPE_CHECKING:
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class TurnRecorder:
    """Accumulate trusted runtime facts for one top-level turn."""

    user_message: str
    run_metadata: dict[str, Any] | None = None
    assistant_text: str = ""
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    interrupted_tools: list[ToolTraceEntry] = field(default_factory=list)
    interruption_reason: str | None = None
    outcome: str = "pending"
    interrupted_persisted: bool = False

    def set_run_metadata(self, metadata: dict[str, Any] | None) -> None:
        """Replace the current Matrix run metadata snapshot."""
        self.run_metadata = dict(metadata) if metadata is not None else None

    def append_assistant_text(self, text: str) -> None:
        """Append one assistant text delta."""
        if text:
            self.assistant_text += text

    def set_assistant_text(self, text: str) -> None:
        """Replace the canonical assistant text observed so far."""
        self.assistant_text = text

    def set_completed_tools(self, tools: list[ToolTraceEntry]) -> None:
        """Replace the completed tool list."""
        self.completed_tools = list(tools)

    def set_interrupted_tools(self, tools: list[ToolTraceEntry]) -> None:
        """Replace the in-flight interrupted tool list."""
        self.interrupted_tools = list(tools)

    def mark_completed(self) -> None:
        """Record successful completion."""
        self.outcome = "completed"

    def mark_interrupted(self, reason: str | None) -> None:
        """Record interruption with one canonical reason string."""
        self.outcome = "interrupted"
        self.interruption_reason = reason or "Run interrupted"

    def interrupted_snapshot(self) -> InterruptedReplaySnapshot:
        """Build one canonical interrupted snapshot from the recorded facts."""
        return build_interrupted_replay_snapshot(
            user_message=self.user_message,
            partial_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
            run_metadata=self.run_metadata,
            interruption_reason=self.interruption_reason or "Run interrupted",
        )

    def claim_interrupted_persistence(self) -> bool:
        """Return whether one interrupted turn should be persisted now."""
        if self.outcome != "interrupted" or self.interrupted_persisted:
            return False
        self.interrupted_persisted = True
        return True
