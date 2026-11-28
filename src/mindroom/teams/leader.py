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
    from mindroom.config.main import Config
    from mindroom.delegation.cost import SubagentCost
    from mindroom.history.types import HistoryScope, PreparedHistoryState, ResolvedReplayPlan
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import RunRecord, TeamRunRecord
    from mindroom.reply_ai_run_progress import ReplyAiRunProgress
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


def _update_team_ai_run_progress(
    live_ai_run: ReplyAiRunProgress,
    *,
    config: Config,
    model_name: str,
    leader_record: TeamRunRecord,
    leader_messages: Sequence[Message],
    member_runs: Sequence[RunRecord],
    prepared_history: PreparedHistoryState | None,
) -> None:
    """Refresh ``live_ai_run`` from leader + deduplicated member runs, keeping leader-only model and context."""
    members = tuple({member.run_id: member for member in member_runs}.values())
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


def team_usage_progress_hook(
    live_ai_run: ReplyAiRunProgress,
    *,
    config: Config,
    model_name: str,
    prepared_history: PreparedHistoryState | None,
) -> TeamUsageHook:
    """Aggregate live leader + member usage/tools into ``live_ai_run`` while keeping leader-only model and context."""

    def _note_team_usage(
        leader_record: TeamRunRecord,
        leader_messages: Sequence[Message],
        live_members: Sequence[RunRecord],
    ) -> None:
        _update_team_ai_run_progress(
            live_ai_run,
            config=config,
            model_name=model_name,
            leader_record=leader_record,
            leader_messages=leader_messages,
            member_runs=live_members,
            prepared_history=prepared_history,
        )

    return _note_team_usage


def seed_team_resume_ai_run(
    live_ai_run: ReplyAiRunProgress | None,
    *,
    config: Config,
    model_name: str,
    persisted: TeamRunRecord,
    subagent_cost: SubagentCost,
) -> TeamUsageHook | None:
    """Seed live provisional metadata from a hydrated paused team run and return its resumed usage hook."""
    if live_ai_run is None:
        return None
    live_ai_run.bind_subagent_cost(subagent_cost)
    _update_team_ai_run_progress(
        live_ai_run,
        config=config,
        model_name=model_name,
        leader_record=persisted,
        leader_messages=persisted.messages,
        member_runs=(*persisted.ended_member_runs, *persisted.member_responses),
        prepared_history=None,
    )
    return team_usage_progress_hook(
        live_ai_run,
        config=config,
        model_name=model_name,
        prepared_history=None,
    )


__all__ = ["LeaderRun", "seed_team_resume_ai_run", "team_usage_progress_hook"]
