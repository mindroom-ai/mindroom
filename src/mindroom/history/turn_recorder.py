"""The record of how one top-level turn ended."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.tool_system.events import ToolTraceEntry

type _TurnOutcome = Literal["pending", "completed", "skipped", "suspended", "interrupted"]


@dataclass
class TurnRecorder:
    """How one top-level turn ended: the run that holds its record, the Matrix metadata it ran with, and its calls.

    Only the turn driver writes it; callers read it once the turn ended. An interrupted turn's history is the run its
    loop stored, which ``run_id`` names and the driver marks ``INTERRUPTED``.
    """

    run_metadata: dict[str, Any] | None = None
    run_id: str | None = None
    # The calls a completed turn finished.
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    outcome: _TurnOutcome = "pending"

    def start(self, *, run_metadata: Mapping[str, Any]) -> None:
        """Seed the record of a turn that starts with the Matrix metadata it runs with."""
        self.run_metadata = dict(run_metadata)

    def record_completed(
        self,
        *,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        completed_tools: Sequence[ToolTraceEntry],
    ) -> None:
        """Record a turn that completed."""
        self._record(run_id, run_metadata)
        self.completed_tools = list(completed_tools)
        self.outcome = "completed"

    def record_interrupted(self, *, run_id: str | None, run_metadata: Mapping[str, Any] | None) -> None:
        """Record a turn a cancellation or a failure cut short."""
        self._record(run_id, run_metadata)
        self.outcome = "interrupted"

    def _record(self, run_id: str | None, run_metadata: Mapping[str, Any] | None) -> None:
        self.run_id = run_id or self.run_id
        if run_metadata is not None:
            self.run_metadata = dict(run_metadata)

    def mark_skipped(self) -> None:
        """Record quiet nonparticipation without success or interrupted replay."""
        self.outcome = "skipped"

    def mark_suspended(self) -> None:
        """Record a native pause without classifying it as terminal."""
        self.outcome = "suspended"
