"""One attempt of a team run, a turn's or a resumed run's: the reply document its events build and what it produced.

The attempt keeps the text each slot received without tool markers, which an interrupted turn records and a blocking
reply delivers, collects the records the run hands over, and renders the run's metadata from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.response_turn import TurnPartialSnapshot
from mindroom.teams.presentation import apply_team_event, render_team_member_slots
from mindroom.teams.runtime import MemberEvent, MemberRunEnded, team_usage
from mindroom.teams.storage import TeamRunRecords
from mindroom.timing import emit_timing_event
from mindroom.tool_system.events import StructuredStreamChunk

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
    from mindroom.teams.members import ResolvedExactTeamMembers
    from mindroom.teams.presentation import TeamStreamPresentation
    from mindroom.teams.runtime import TeamRunEvent
    from mindroom.tool_system.events import StreamingToolTracker


def team_tool_executions(record: TeamRunRecord, members: Sequence[RunRecord]) -> list[ToolExecution]:
    """Return the leader's and every member's tool executions.

    Any member's completed model-switch call surfaces here and drives a whole-fan continuation, even when the team
    already produced visible output, as it does for an agent: gating on visible output would disable continuation
    in coordinate mode, where the leader almost always synthesizes text around a member's truncated tool call.
    """
    return [*(record.tools or ()), *(tool for member in members for tool in member.tools or ())]


@dataclass
class TeamAttempt:
    """One attempt of a team turn: its prepared input, its document, and what the run produced so far."""

    run_id: str
    members: ResolvedExactTeamMembers
    presentation: TeamStreamPresentation
    show_tool_calls: bool
    team_name: str
    runtime_model_name: str
    # The history preparation of a new run; a resume prepares none.
    prepared_history: PreparedHistoryState | None
    # The text each slot received, without tool markers: what an interrupted turn records and a blocking reply delivers.
    canonical_per_member: dict[str, str]
    canonical_consensus: str = ""
    # The slots in the order their members' runs first ended, which a blocking reply lists them in.
    ended_member_ids: list[str] = field(default_factory=list)
    records: TeamRunRecords = field(default_factory=TeamRunRecords)

    @property
    def tool_tracker(self) -> StreamingToolTracker:
        """The tracker of the document's tool calls, completed and pending."""
        return self.presentation.tool_tracker

    def canonical_text(self) -> str:
        """Return the reply's text so far without display chrome: the member slots and the consensus."""
        return self._render_slots(self.presentation.member_ids)

    def completed_text(self) -> str:
        """Return the text a blocking reply delivers, without display chrome: the member slots and the consensus.

        The members come in the order their runs ended, as Agno's blocking reply listed them, while the streamed
        document keeps the configured order.
        """
        ended = self.ended_member_ids
        return self._render_slots(
            [*ended, *(member_id for member_id in self.presentation.member_ids if member_id not in ended)],
        )

    def _render_slots(self, member_ids: Sequence[str]) -> str:
        return "\n\n".join(
            render_team_member_slots(
                member_ids,
                display_names_by_id=self.presentation.display_names_by_id,
                per_member=self.canonical_per_member,
                consensus=self.canonical_consensus,
            ),
        )

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the live partial-output view of this attempt, for a turn cancelled from outside."""
        return TurnPartialSnapshot(
            assistant_text=self.canonical_text(),
            completed_tools=tuple(self.tool_tracker.completed_tools),
            interrupted_tools=tuple(pending.trace_entry for pending in self.tool_tracker.pending_tools),
            attempt_run_id=self.run_id,
        )

    def apply(self, event: TeamRunEvent) -> bool:
        """Fold one run event into the attempt; return whether the document changed."""
        if isinstance(event, MemberRunEnded):
            member_id = self.presentation.resolve_member_id(event.member_id, event.member_name)
            if member_id not in self.ended_member_ids:
                self.ended_member_ids.append(member_id)
        if self.records.collect(event):
            return False
        if isinstance(event, MemberEvent):
            inner = event.event
            if isinstance(inner, ContentDelta):
                member_id = self.presentation.resolve_member_id(event.member_id, event.member_name)
                self.canonical_per_member[member_id] += inner.text
            elif isinstance(inner, ToolStarted | ToolCompleted):
                self._emit_tool_timing(inner, tool_scope="member", agent_name=event.member_name)
            else:
                return False
        elif isinstance(event, ContentDelta):
            self.canonical_consensus += event.text
        elif isinstance(event, ToolStarted | ToolCompleted):
            self._emit_tool_timing(event, tool_scope="team", agent_name=None)
        else:
            return False
        apply_team_event(self.presentation, event)
        return True

    def _emit_tool_timing(
        self,
        event: ToolStarted | ToolCompleted,
        *,
        tool_scope: Literal["member", "team"],
        agent_name: str | None,
    ) -> None:
        emit_timing_event(
            "Dispatch tool-call timing",
            phase="tool_call_started" if isinstance(event, ToolStarted) else "tool_call_completed",
            team_name=self.team_name,
            tool_scope=tool_scope,
            agent_name=agent_name,
            tool_name=event.tool.tool_name,
            tool_call_id=event.tool.tool_call_id,
            show_tool_calls=self.show_tool_calls,
        )

    def chunk(self) -> StructuredStreamChunk | None:
        """Return the rendered document as a stream chunk, or None while it is still empty."""
        rendered_body = self.presentation.render_body()
        if not rendered_body:
            return None
        return StructuredStreamChunk(
            content=rendered_body,
            tool_trace=self.presentation.tool_trace.copy(),
            presentation_state=self.presentation.to_state(),
        )

    def metadata_content(self, config: Config, *, session_id: str, status: RunStatus) -> dict[str, Any]:
        """Return the run metadata payload: the leader's identity, the team's usage, and the prepared history."""
        record = self.records.record
        prepared_history = self.prepared_history
        return build_ai_run_metadata_content(
            config=config,
            model_name=self.runtime_model_name,
            run_id=record.run_id if record is not None else self.run_id,
            session_id=(record.session_id if record is not None else None) or session_id,
            status=status,
            model=record.model if record is not None else None,
            model_provider=record.model_provider if record is not None else None,
            metrics=team_usage(record, self.records.members) if record is not None else None,
            context_metrics=record.metrics if record is not None else None,
            context_input_tokens=prepared_history.prepared_context_tokens if prepared_history is not None else None,
            tool_count=len(team_tool_executions(record, self.records.members)) if record is not None else 0,
            prepared_history=prepared_history,
        )


__all__ = ["TeamAttempt", "team_tool_executions"]
