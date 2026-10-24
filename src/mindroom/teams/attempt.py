"""One attempt of a team run, a turn's or a resumed run's: the reply document its events build and what it produced.

A streamed attempt records ``TeamReplyText``, the reply's text without tool markers, however it ends; a blocking
attempt renders and records its reply from the records the run hands over, which give every attempt's run metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.budgets.pricing import model_prices
from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.response_turn import TurnPartialSnapshot
from mindroom.teams.presentation import apply_team_event, format_team_response, render_team_member_slots
from mindroom.teams.runtime import MemberEvent, team_usage
from mindroom.teams.storage import TeamRunRecords
from mindroom.timing import emit_timing_event
from mindroom.tool_system.events import StructuredStreamChunk, append_stream_text

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.teams.members import ResolvedExactTeamMembers
    from mindroom.teams.presentation import TeamStreamPresentation
    from mindroom.teams.runtime import TeamRunEvent


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
    unless that is not its content: a failed run hands over why it failed. What a resumed run carried over from its
    pause the document the resume continues already accounts for.
    """
    handover = run.new_handover_content
    return None if streamed and handover == run.content else handover


# TODO(simplify): A second copy of each slot's text joins text around a hidden tool call directly, so a streamed reply
# records "Looking.Four seats." where it showed two paragraphs: the streamed side of #3075's blocking-reply join.
# Record the shown document's text without tool markers, one copy. Replayed history and models would notice.
@dataclass
class TeamReplyText:
    """The text of one team reply without tool markers: each member's text and the consensus, as the run's events build it.

    ``presentation`` owns the member identities: the configured slot order, the display names, and the slot an event's
    member resolves to. A member's deltas join directly, so a hidden tool call between them leaves no gap, as the
    run's own content joins them. ``streamed_member_runs`` names the member runs whose deltas gave text, by run id,
    since a member's slot holds the text of each of its runs.
    """

    presentation: TeamStreamPresentation
    per_member: dict[str, str] = field(init=False)
    consensus: str = ""
    streamed_member_runs: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        """Give every configured slot empty text."""
        self.per_member = dict.fromkeys(self.presentation.member_ids, "")

    def apply(self, event: TeamRunEvent) -> None:
        """Fold one run event into the text: a member's or the leader's delta."""
        if isinstance(event, MemberEvent):
            if isinstance(event.event, ContentDelta):
                member_id = self.presentation.resolve_member_id(event.member_id, event.member_name)
                self.per_member[member_id] += event.event.text
                if event.event.text:
                    self.streamed_member_runs.add(event.run_id)
        elif isinstance(event, ContentDelta):
            self.consensus += event.text

    def canonical_text(self) -> str:
        """Return the text in the configured member order, then the consensus: what a streamed reply records."""
        return "\n\n".join(
            render_team_member_slots(
                self.presentation.member_ids,
                display_names_by_id=self.presentation.display_names_by_id,
                per_member=self.per_member,
                consensus=self.consensus,
            ),
        )


@dataclass
class TeamAttempt:
    """One attempt of a team turn: its prepared input, its document, and what the run produced so far."""

    run_id: str
    members: ResolvedExactTeamMembers
    presentation: TeamStreamPresentation
    # Whether the document streams as it grows; a blocking turn renders its reply from the run's records instead.
    stream: bool
    team_name: str
    runtime_model_name: str
    # The history preparation of a new run; a resume prepares none.
    prepared_history: PreparedHistoryState | None
    # The reply without tool markers: what a streamed turn records.
    reply: TeamReplyText = field(init=False)
    records: TeamRunRecords = field(default_factory=TeamRunRecords)

    def __post_init__(self) -> None:
        """Start the reply's text from the presentation's member slots."""
        self.reply = TeamReplyText(self.presentation)

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
        """Return the text the turn records when this attempt is stopped or fails.

        A streamed attempt records its reply's text (``TeamReplyText``); a blocking one its stored runs' document,
        stripped, or nothing before the run stored a record, kept as main behaves. A stopped leader that wrote nothing
        contributes nothing; a failed one contributes the reason its run recorded, credentials redacted.
        """
        if self.stream:
            return self.reply.canonical_text()
        record = self.records.record
        return format_team_response(record).strip() if record is not None else ""

    def apply(self, event: TeamRunEvent) -> bool:
        """Fold one run event into the attempt; return whether the document changed."""
        self.reply.apply(event)
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
                tool_name=inner.tool.tool_name,
                tool_call_id=inner.tool.tool_call_id,
                show_tool_calls=self.presentation.show_tool_calls,
            )
        elif not isinstance(inner, ContentDelta):
            return False
        apply_team_event(self.presentation, event)
        return True

    def show_unshown_handover(self, run: AnyRunRecord) -> bool:
        """Add to ``run``'s slot what the run hands over and its events did not show; return whether anything was added.

        The team run is the leader, whose text only the consensus holds; a member run goes to its member's slot and is
        matched by its run id, so an earlier run of the same member keeps its text without standing for a later one.
        The addition starts a paragraph of its own, in the document and in the reply's text alike.
        """
        reply = self.reply
        if isinstance(run, TeamRunRecord):
            handover = _unshown_handover(run, streamed=bool(reply.consensus))
            self.presentation.append_consensus(handover, separate=True)
            reply.consensus = append_stream_text(reply.consensus, handover, separate=True)
        else:
            member_id = self.presentation.resolve_member_id(run.agent_id, run.agent_name)
            handover = _unshown_handover(run, streamed=run.run_id in reply.streamed_member_runs)
            self.presentation.append_member(member_id, handover, separate=True)
            reply.per_member[member_id] = append_stream_text(reply.per_member[member_id], handover, separate=True)
        return bool(handover)

    def show_failure_reasons(self, record: TeamRunRecord) -> bool:
        """Add the reason each failed member run of ``record`` recorded, credentials redacted, that its deltas did not show.

        A live streamed turn shows its runs' deltas and, when the team run ends, each failed member run's reason, as a
        resumed reply shows it (``show_unshown_handover``). Return whether any reason was added.
        """
        shown = False
        for member in record.ended_member_runs:
            if member.status is RunStatus.error:
                shown = self.show_unshown_handover(member) or shown
        return shown

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

    def metadata_content(
        self,
        config: Config,
        *,
        runtime_paths: RuntimePaths,
        session_id: str,
        status: RunStatus,
    ) -> dict[str, Any]:
        """Return the stored run's metadata payload: the leader's identity, the team's usage, and the prepared history."""
        record = self.records.terminal(self.team_name)
        prepared_history = self.prepared_history
        return build_ai_run_metadata_content(
            config=config,
            prices=model_prices(config, runtime_paths),
            model_name=self.runtime_model_name,
            run_id=record.run_id,
            session_id=record.session_id or session_id,
            status=status,
            model=record.model,
            model_provider=record.model_provider,
            metrics=team_usage(record, record.ended_member_runs),
            context_metrics=record.metrics,
            context_input_tokens=prepared_history.prepared_context_tokens if prepared_history is not None else None,
            tool_count=len(team_tool_executions(record)),
            prepared_history=prepared_history,
        )


__all__ = ["TeamAttempt", "TeamReplyText", "team_tool_executions"]
