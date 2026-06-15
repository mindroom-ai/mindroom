"""AI response turn-state helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class AITurnState:
    """Apply one AI response attempt's visible state to the top-level turn."""

    prior_completed_tools: Sequence[ToolTraceEntry] = ()
    assistant_text: str = ""
    completed_tools: list[ToolTraceEntry] = field(default_factory=list, init=False)
    interrupted_tools: list[ToolTraceEntry] = field(default_factory=list, init=False)

    def completed_tools_for(self, attempt_completed_tools: Sequence[ToolTraceEntry]) -> list[ToolTraceEntry]:
        """Return the top-level completed tool trace for one attempt."""
        return [*self.prior_completed_tools, *attempt_completed_tools]

    def sync_partial(
        self,
        recorder: TurnRecorder | None,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Refresh the live top-level turn state without deciding an outcome."""
        self.assistant_text = assistant_text
        self.completed_tools = self.completed_tools_for(completed_tools)
        self.interrupted_tools = list(interrupted_tools)
        if recorder is None:
            return
        recorder.sync_partial_state(
            run_metadata=run_metadata,
            assistant_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
        )

    def record_completed(
        self,
        recorder: TurnRecorder | None,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record a completed top-level turn when a recorder is present."""
        self.assistant_text = assistant_text
        self.completed_tools = self.completed_tools_for(completed_tools)
        self.interrupted_tools = []
        if recorder is None:
            return
        recorder.record_completed(
            run_metadata=run_metadata,
            assistant_text=self.assistant_text,
            completed_tools=self.completed_tools,
        )

    def record_interrupted(
        self,
        recorder: TurnRecorder | None,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record an interrupted top-level turn when a recorder is present."""
        self.assistant_text = assistant_text
        self.completed_tools = self.completed_tools_for(completed_tools)
        self.interrupted_tools = list(interrupted_tools)
        if recorder is None:
            return
        recorder.record_interrupted(
            run_metadata=run_metadata,
            assistant_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
        )

    def record_interrupted_from_recorder(
        self,
        recorder: TurnRecorder,
        *,
        run_metadata: Mapping[str, Any] | None,
    ) -> None:
        """Mark the recorder interrupted using its already-canonical live state."""
        self.assistant_text = recorder.assistant_text
        self.completed_tools = list(recorder.completed_tools)
        self.interrupted_tools = list(recorder.interrupted_tools)
        recorder.record_interrupted(
            run_metadata=run_metadata,
            assistant_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
        )
