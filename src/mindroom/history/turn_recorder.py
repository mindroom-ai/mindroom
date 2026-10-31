"""The record of one top-level turn, which canonical interrupted replay is built from."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.history.interrupted_replay import (
    NO_TURN_INPUT,
    InterruptedReplaySnapshot,
    TurnInput,
    build_interrupted_replay_snapshot,
)
from mindroom.llm.runs import RunStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mindroom.tool_system.events import ToolTraceEntry

type _TurnOutcome = Literal["pending", "completed", "skipped", "suspended", "interrupted"]


@dataclass
class TurnRecorder:
    """What one top-level turn was asked, how it ended, and what an interruption keeps of it.

    Only the turn driver writes it, besides the run's mid-turn compaction, which notes the calls a fold summarized, and
    a voice call, which seeds the transcript it answers; callers read it once the turn ended.
    """

    user_message: str = ""
    user_message_is_structured: bool = False
    run_metadata: dict[str, Any] | None = None
    run_id: str | None = None
    assistant_text: str = ""
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    interrupted_tools: list[ToolTraceEntry] = field(default_factory=list)
    outcome: _TurnOutcome = "pending"
    original_status: RunStatus | None = None
    # Tool calls a mid-turn compaction folded into its summary; an interrupted record leaves these calls out.
    folded_tool_call_ids: set[str] = field(default_factory=set)
    # The input around the prompt that an interrupted turn's run stores, which its record keeps.
    turn_input: TurnInput = NO_TURN_INPUT

    def start(self, *, user_message: str, user_message_is_structured: bool, run_metadata: Mapping[str, Any]) -> None:
        """Seed the record of a turn that starts: its user message, and the Matrix metadata it runs with."""
        self.user_message = user_message
        self.user_message_is_structured = user_message_is_structured
        self.run_metadata = dict(run_metadata)

    def record_folded_tool_calls(self, tool_call_ids: Iterable[str]) -> None:
        """Remember tool calls a mid-turn compaction folded, so an interrupted record keeps only the work after it."""
        self.folded_tool_call_ids.update(tool_call_ids)

    def record_completed(
        self,
        *,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record a turn that completed."""
        self._record(run_id, run_metadata, assistant_text, completed_tools, ())
        self.outcome = "completed"

    def record_interrupted(
        self,
        *,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
        original_status: RunStatus,
        turn_input: TurnInput = NO_TURN_INPUT,
    ) -> None:
        """Record a turn a cancellation or a failure cut short, with the partial output its attempt had.

        ``turn_input`` is its input around the prompt as the attempt's run stores it.
        """
        self._record(run_id, run_metadata, assistant_text, completed_tools, interrupted_tools)
        self.turn_input = turn_input
        self.outcome = "interrupted"
        self.original_status = original_status

    def _record(
        self,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        self.run_id = run_id or self.run_id
        if run_metadata is not None:
            self.run_metadata = dict(run_metadata)
        self.assistant_text = assistant_text
        self.completed_tools = list(completed_tools)
        self.interrupted_tools = list(interrupted_tools)

    def mark_skipped(self) -> None:
        """Record quiet nonparticipation without success or interrupted replay."""
        self.outcome = "skipped"

    def mark_suspended(self) -> None:
        """Record a native pause without classifying it as terminal."""
        self.outcome = "suspended"

    def interrupted_snapshot(self, *, response_event_id: str | None = None) -> InterruptedReplaySnapshot:
        """Build one canonical interrupted snapshot from the recorded facts and the event the reply shows."""
        return build_interrupted_replay_snapshot(
            user_message=self.user_message,
            user_message_is_structured=self.user_message_is_structured,
            partial_text=self.assistant_text,
            completed_tools=self.completed_tools,
            interrupted_tools=self.interrupted_tools,
            run_metadata=self.run_metadata,
            response_event_id=response_event_id,
            original_status=self.original_status or RunStatus.cancelled,
            folded_tool_call_ids=self.folded_tool_call_ids,
            turn_input=self.turn_input,
        )
