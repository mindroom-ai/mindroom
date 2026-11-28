"""How a team's leader runs in one turn or resume: the team, its storage, hooks, request settings, and model.

A live turn and a saved approval's resume each build one ``LeaderRun``; a turn starts the leader's run from it, and a
resume continues a paused one, so both run the leader the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.spec import RunContext
from mindroom.ai_run_metadata import prior_usage_by_run
from mindroom.approval.resume import resume_inputs
from mindroom.llm.usage import team_run_usage
from mindroom.run_storage import persist_into
from mindroom.teams.runtime import continue_team, run_team
from mindroom.tool_jobs.consumption import acknowledge_saved_run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.ai_run_metadata import ReplyAiRunProgress
    from mindroom.config.main import Config
    from mindroom.history.types import HistoryScope, PreparedHistoryState, ResolvedReplayPlan
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import RunRecord, TeamRunRecord
    from mindroom.run_storage import RunStorage, TerminalRecord
    from mindroom.teams.members import BuiltTeam
    from mindroom.teams.runtime import TeamDecisions, TeamRunEvent, TeamUsageHook


@dataclass(frozen=True)
class LeaderRun:
    """How the leader of ``built`` runs: where its run stores, its hooks, its request settings, and its model.

    ``model_name`` is the configured model the team run uses, which its tool calls carry. Every run shares the
    stored session's ``session_state`` with its tools, which the store writes back, and ``compaction_requests``
    across ``start`` and ``resume`` so a member's mid-turn compaction request survives a delegation pause.
    """

    built: BuiltTeam
    storage: RunStorage
    hooks: LoopHooks
    request_template: ModelRequest
    model_name: str
    compaction_requests: set[HistoryScope] = field(default_factory=set)
    on_usage_updated: TeamUsageHook | None = None

    def start(
        self,
        run_input: Sequence[Message],
        *,
        run_id: str,
        user_id: str | None,
        metadata: dict[str, Any] | None,
        records: TerminalRecord[TeamRunRecord] | None,
    ) -> AsyncIterator[TeamRunEvent]:
        """Start the leader's run over ``run_input``."""
        return run_team(
            self.built.spec,
            run_input,
            ctx=RunContext(
                run_id=run_id,
                session=self.storage.session,
                user_id=user_id,
                metadata=metadata,
                history_scope=self.storage.scope,
                model_name=self.model_name,
                compaction_requests=self.compaction_requests,
            ),
            hooks=self.hooks,
            persist=persist_into(records, self.storage, on_saved=acknowledge_saved_run),
            request_template=self.request_template,
            on_usage_updated=self.on_usage_updated,
        )

    def resume(
        self,
        paused: TeamRunRecord,
        decisions: TeamDecisions,
        *,
        plan: ResolvedReplayPlan | None,
        user_id: str | None,
        records: TerminalRecord[TeamRunRecord],
    ) -> AsyncIterator[TeamRunEvent]:
        """Continue ``paused`` with ``decisions``, its leader's request replaying the stored history ``plan`` selects."""
        request, context = resume_inputs(
            paused,
            self.storage,
            self.built.history_settings,
            plan,
            user_id=user_id,
            model_name=self.model_name,
            history_scope=self.storage.scope,
            compaction_requests=self.compaction_requests,
        )
        return continue_team(
            self.built.spec,
            paused,
            request,
            decisions=decisions,
            context=context,
            hooks=self.hooks,
            persist=persist_into(records, self.storage, on_saved=acknowledge_saved_run),
            request_template=self.request_template,
            on_usage_updated=self.on_usage_updated,
        )


def team_usage_progress_hook(
    live_ai_run: ReplyAiRunProgress,
    *,
    config: Config,
    model_name: str,
    prepared_history: PreparedHistoryState | None,
    prior_member_runs: Sequence[RunRecord] = (),
) -> TeamUsageHook:
    """Aggregate live leader + member usage/tools into ``live_ai_run`` while keeping leader-only model and context."""
    prior_by_id = {member.run_id: member for member in prior_member_runs}

    def _note_team_usage(
        leader_record: TeamRunRecord,
        leader_messages: Sequence[Message],
        live_members: Sequence[RunRecord],
    ) -> None:
        merged_members = {**prior_by_id, **{member.run_id: member for member in live_members}}
        members = tuple(merged_members.values())
        live_ai_run.update_from_run(
            config,
            model_name=model_name,
            record=leader_record,
            messages=leader_messages,
            prior_usage=tuple(prior_usage_by_run(leader_record.metadata).values()),
            prepared_history=prepared_history,
            metrics_override=team_run_usage(leader_record.metrics, (member.metrics for member in members)),
            tool_count_override=len(leader_record.tools or ()) + sum(len(member.tools or ()) for member in members),
        )

    return _note_team_usage


__all__ = ["LeaderRun", "team_usage_progress_hook"]
