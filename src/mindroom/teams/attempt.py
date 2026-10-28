"""One attempt of a team run, a turn's or a resumed run's: the reply document its events build and what it produced.

An attempt records its document's text without tool markers, however it ends, and pauses showing that document; the
records the run hands over give its run metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.budgets.pricing import model_prices
from mindroom.llm.events import TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.response_turn import TurnPartialSnapshot
from mindroom.run_storage import TerminalRecord
from mindroom.teams.presentation import apply_team_event
from mindroom.teams.runtime import MemberEvent, team_usage
from mindroom.timing import emit_timing_event
from mindroom.tool_system.events import StructuredStreamChunk

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.teams.members import ResolvedExactTeamMembers
    from mindroom.teams.presentation import TeamStreamPresentation
    from mindroom.teams.runtime import TeamRunEvent
    from mindroom.teams.spec import TeamSpec


def team_tool_executions(record: TeamRunRecord) -> list[ToolExecution]:
    """Return the leader's and every ended member run's tool executions.

    Any member's completed model-switch call surfaces here and drives a whole-fan continuation, even when the team
    already produced visible output, as it does for an agent: gating on visible output would disable continuation
    in coordinate mode, where the leader almost always synthesizes text around a member's truncated tool call.
    """
    return [*(record.tools or ()), *(tool for member in record.ended_member_runs for tool in member.tools or ())]


def _unshown_handover(run: AnyRunRecord, *, streamed: bool) -> str | None:
    """Return what ``run`` hands over beyond its pause (``new_handover_content``) that its events have not shown.

    A run's deltas show its content as it is written, so a run that streamed text has shown what it hands over,
    unless it failed: a failed run hands over why it failed, which no delta shows, even when its content is that
    reason because its model said nothing it kept. What a resumed run carried over from its pause the document the
    resume continues already accounts for. A paused run shows nothing more: beyond the text it streamed, its content
    is a paused notice, which a team reply never shows, as a pause of a live turn does not.
    """
    if run.status is RunStatus.paused:
        return None
    handover = run.new_handover_content
    if run.status is RunStatus.error:
        return handover
    return None if streamed and handover == run.content else handover


@dataclass
class TeamAttempt:
    """One attempt of a team turn or resumed run: its members, its document, and what the run produced so far.

    ``team`` is the team the attempt runs, whose built members own the calls a pause waits on (``TeamSpec.tool_owners``).
    """

    run_id: str
    members: ResolvedExactTeamMembers
    team: TeamSpec
    presentation: TeamStreamPresentation
    team_name: str
    runtime_model_name: str
    # The history preparation of a new run; a resume prepares none.
    prepared_history: PreparedHistoryState | None
    records: TerminalRecord[TeamRunRecord] = field(default_factory=partial(TerminalRecord, TeamRunRecord))
    # Whether the leader's deltas gave text, and the member runs whose deltas did, by run id: a member's slot holds the
    # text of each of its runs.
    leader_streamed: bool = field(default=False, init=False)
    streamed_member_runs: set[str] = field(default_factory=set, init=False)

    def terminal_record(self) -> TeamRunRecord:
        """Return the team run's terminal record, which the run hands over before its events end."""
        return self.records.terminal(self.team_name)

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the live partial-output view of this attempt: what a stopped or failed turn records."""
        tracker = self.presentation.tool_tracker
        return TurnPartialSnapshot(
            assistant_text=self.interrupted_text(),
            completed_tools=tuple(tracker.completed_tools),
            interrupted_tools=tuple(pending.trace_entry for pending in tracker.pending_tools),
            attempt_run_id=self.run_id,
        )

    def interrupted_text(self) -> str:
        """Return the text the turn records when this attempt is stopped or fails: its document without tool markers.

        The text ends with the no-consensus note only once the team run completed, so it matches the message a stopped
        reply shows.
        """
        record = self.records.record
        return self.presentation.text_without_tool_markers(
            finished=record is not None and record.status is RunStatus.completed,
        )

    def apply(self, event: TeamRunEvent) -> bool:
        """Fold one run event into the attempt; return whether the document changed."""
        if isinstance(event, MemberEvent) and isinstance(event.event, TextDelta) and event.event.text:
            self.streamed_member_runs.add(event.run_id)
        elif isinstance(event, TextDelta) and event.text:
            self.leader_streamed = True
        if self.records.collect(event):
            return False
        member = event if isinstance(event, MemberEvent) else None
        inner = member.event if member is not None else event
        if isinstance(inner, ToolStarted | ToolCompleted):
            emit_timing_event(
                "Dispatch tool-call timing",
                phase="tool_call_started" if isinstance(inner, ToolStarted) else "tool_call_completed",
                team_name=self.team_name,
                tool_scope="member" if member is not None else "team",
                agent_name=member.member_name if member is not None else None,
                tool_name=inner.call.tool_name,
                tool_call_id=inner.call.tool_call_id,
                show_tool_calls=self.presentation.show_tool_calls,
            )
        elif not isinstance(inner, TextDelta):
            return False
        apply_team_event(self.presentation, event)
        return True

    def show_unshown_handover(self, run: AnyRunRecord) -> None:
        """Add to ``run``'s slot what the run hands over and its events did not show.

        The team run is the leader, whose text only the consensus holds; a member run goes to its member's slot and is
        matched by its run id, so an earlier run of the same member keeps its text without standing for a later one.
        The addition starts a paragraph of its own.
        """
        if isinstance(run, TeamRunRecord):
            handover = _unshown_handover(run, streamed=self.leader_streamed)
            self.presentation.append_consensus(handover, separate=True)
        else:
            member_id = self.presentation.resolve_member_id(run.agent_id, run.agent_display_name)
            handover = _unshown_handover(run, streamed=run.run_id in self.streamed_member_runs)
            self.presentation.append_member(member_id, handover, separate=True)

    def show_failure_reasons(self, record: TeamRunRecord) -> None:
        """Add the reason each failed run of ``record``, the team run or a member run, recorded, credentials redacted.

        A turn shows its runs' deltas and, once the team run ends however it ends, each failed run's reason, as a
        resumed reply shows it (``show_unshown_handover``): a failed leader's in the consensus, a failed member's in
        its slot.
        """
        for run in (record, *record.ended_member_runs):
            if run.status is RunStatus.error:
                self.show_unshown_handover(run)

    def chunk(self, *, finished: bool) -> StructuredStreamChunk | None:
        """Return the rendered document as a stream chunk, or None while it is still empty.

        A live chunk shows the run still running; the ``finished`` one shows the reply its completed run ends with.
        """
        rendered_body = self.presentation.render_body(finished=finished)
        if not rendered_body:
            return None
        return StructuredStreamChunk(
            content=rendered_body,
            tool_trace=self.presentation.tool_trace.copy(),
            presentation_state=self.presentation.to_state(),
        )

    def metadata_content(
        self,
        config: Config,
        *,
        runtime_paths: RuntimePaths,
        session_id: str,
        status: RunStatus,
    ) -> dict[str, Any]:
        """Return the stored run's metadata payload: the leader's identity, the team's usage, and the prepared history.

        The context reported is the leader's last request's, since each member's requests held a context of their own.
        """
        record = self.terminal_record()
        return build_ai_run_metadata_content(
            config=config,
            prices=model_prices(config, runtime_paths),
            model_name=self.runtime_model_name,
            run_id=record.run_id,
            session_id=session_id,
            status=status,
            model=record.model,
            model_provider=record.model_provider,
            metrics=team_usage(record),
            context_run=record,
            tool_count=len(team_tool_executions(record)),
            prepared_history=self.prepared_history,
        )


__all__ = ["TeamAttempt", "team_tool_executions"]
