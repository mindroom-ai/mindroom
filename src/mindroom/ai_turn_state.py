"""AI response turn-state helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.run.base import RunStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.tool_system.events import ToolTraceEntry


def merge_tool_trace_snapshots(
    canonical: Sequence[ToolTraceEntry],
    supplemental: Sequence[ToolTraceEntry],
) -> list[ToolTraceEntry]:
    """Merge cumulative tool snapshots without dropping or duplicating overlap."""
    canonical_list = list(canonical)
    supplemental_list = list(supplemental)
    if not canonical_list or not supplemental_list:
        return canonical_list or supplemental_list
    if canonical_list[: len(supplemental_list)] == supplemental_list:
        return canonical_list
    if supplemental_list[: len(canonical_list)] == canonical_list:
        return supplemental_list
    shared_prefix = 0
    while (
        shared_prefix < min(len(canonical_list), len(supplemental_list))
        and canonical_list[shared_prefix] == supplemental_list[shared_prefix]
    ):
        shared_prefix += 1
    if shared_prefix:
        return [*canonical_list, *supplemental_list[shared_prefix:]]
    max_overlap = min(len(canonical_list), len(supplemental_list))
    for overlap in range(max_overlap, 0, -1):
        if canonical_list[-overlap:] == supplemental_list[:overlap]:
            supplemental_list = supplemental_list[overlap:]
            break
    return [*canonical_list, *supplemental_list]


def merge_text_snapshots(canonical: str, supplemental: str) -> str:
    """Merge cumulative partial-text snapshots while preserving divergent fragments."""
    if not canonical:
        return supplemental
    if not supplemental:
        return canonical
    if canonical.startswith(supplemental):
        return canonical
    if supplemental.startswith(canonical):
        return supplemental
    max_overlap = min(len(canonical), len(supplemental))
    for overlap in range(max_overlap, 0, -1):
        if canonical[-overlap:] == supplemental[:overlap]:
            return f"{canonical}{supplemental[overlap:]}"
    return f"{canonical}\n\n{supplemental}"


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
        original_status: RunStatus = RunStatus.cancelled,
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
            original_status=original_status,
        )

    def record_interrupted_canonical(
        self,
        recorder: TurnRecorder | None,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
        original_status: RunStatus = RunStatus.cancelled,
    ) -> None:
        """Record already-merged top-level interruption state without re-prefixing."""
        self.assistant_text = assistant_text
        self.completed_tools = list(completed_tools)
        self.interrupted_tools = list(interrupted_tools)
        if recorder is None:
            return
        recorder.record_interrupted(
            run_metadata=run_metadata,
            assistant_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
            original_status=original_status,
        )

    def record_interrupted_from_recorder(
        self,
        recorder: TurnRecorder,
        *,
        run_metadata: Mapping[str, Any] | None,
        original_status: RunStatus | None = None,
    ) -> None:
        """Mark the recorder interrupted using its already-canonical live state."""
        self.record_interrupted_canonical(
            recorder,
            run_metadata=run_metadata,
            assistant_text=recorder.assistant_text,
            completed_tools=recorder.completed_tools,
            interrupted_tools=recorder.interrupted_tools,
            original_status=recorder.interruption_status if original_status is None else original_status,
        )
