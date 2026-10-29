"""Resuming a paused team run: within a turn, after a member's delegations, and from a saved approval.

``TeamResume`` continues a paused run as the turn's leader runs (``LeaderRun``), with the decisions routed to the
member or leader calls the team waits for; ``drive_team_delegations`` runs the children of a member's delegations and
continues the run with their results. ``continue_paused_team_run`` rebuilds the team and its leader run from a saved
approval and resumes its stored run through both.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom import ai_runtime
from mindroom.ai import run_delegated_child_response
from mindroom.approval.receipt_readers import add_approval_receipt_reader
from mindroom.approval.resume import approval_decisions, with_resumed_turn_log_context
from mindroom.approval.tool_owners import require_original_toolkit, required_approval_tool_names
from mindroom.config.resolution import entity_timezone
from mindroom.delegation.execution import drive_delegation_stream, has_delegation_state
from mindroom.delegation.state import member_call_id, member_delegation_card, pending_call_slot
from mindroom.helper_usage import helper_usage_context
from mindroom.history.native import restore_native_history, with_native_history
from mindroom.history.replay import configured_replay_plan
from mindroom.history.session_context import load_scope_session, resolve_bound_team_scope
from mindroom.llm.events import RunFinished
from mindroom.llm.provider import ModelRequest
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.loop_hooks import HookTurn, leader_loop_hooks
from mindroom.response_turn import CompletedApprovalRun, pause_without_approval_error, paused_attempt_from_response
from mindroom.run_storage import scope_run_storage
from mindroom.teams.attempt import TeamAttempt
from mindroom.teams.leader import LeaderRun
from mindroom.teams.members import (
    build_team_spec,
    materialized_team_label,
    resolve_exact_team_members,
)
from mindroom.teams.presentation import (
    NO_TEAM_RESPONSE,
    TeamStreamPresentation,
    format_team_header,
    publish_team_presentation,
)
from mindroom.teams.runtime import MemberEvent, TeamDecisions
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.turn_model import ModelScope, resolve_team_turn_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.agent_loop.run import ApprovedCall
    from mindroom.approval.resume import ResumeDecisions
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import HistoryScope, ResolvedReplayPlan
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.events import RunEvent, ToolCompleted, ToolStarted
    from mindroom.llm.runs import RunRequirement
    from mindroom.response_turn import PausedAttempt
    from mindroom.run_storage import RunStorage, TerminalRecord
    from mindroom.streaming import ProgressPublisher
    from mindroom.teams.runtime import TeamCallKey, TeamRunEvent
    from mindroom.teams.spec import TeamMode, TeamSpec
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _stored_paused_team_run(scope: ScopeSessionContext, run_id: str) -> TeamRunRecord:
    """Return the paused team run the approval names from the session ``scope`` loaded, which holds its paused runs.

    A run of another session of the scope, or of a session with no stored row, is refused, as the agent path refuses it.
    """
    runs = scope.session.runs if scope.session is not None else ()
    persisted = next((run for run in runs if run.run_id == run_id), None)
    if not isinstance(persisted, TeamRunRecord) or persisted.status is not RunStatus.paused:
        msg = f"Paused team run {run_id!r} is no longer available"
        raise RuntimeError(msg)
    return persisted


def _route_decisions(
    persisted: TeamRunRecord,
    decisions: ResumeDecisions,
    *,
    toolkit_owners: Mapping[tuple[str, str], str],
) -> TeamDecisions:
    """Route each decision to the waiting member or leader call it names, checking an approved call's toolkit owner.

    An approved call is matched by the member the approval was shown for, and must still come from the toolkit the
    approval recorded, so a removed or replaced tool cannot run under an old approval. A denial names its call by id
    alone, and a call id two members share is refused rather than guessed at; a delegation's result names its member
    and call, which the resume checks against the calls the team waits for.
    """
    waiting: dict[str, list[RunRequirement]] = {}
    for requirement in persisted.requirements or ():
        if not requirement.is_resolved():
            waiting.setdefault(requirement.tool_execution.tool_call_id, []).append(requirement)

    def key_of(call_id: str) -> TeamCallKey:
        candidates = waiting.get(call_id, [])
        if not candidates:
            msg = "Saved approval calls no longer match the pending requirements; retry the request"
            raise RuntimeError(msg)
        if len(candidates) > 1:
            msg = f"Call {call_id!r} is pending for more than one member of the team; retry the request"
            raise RuntimeError(msg)
        return (candidates[0].member_agent_id, call_id)

    approved: dict[TeamCallKey, ApprovedCall] = {}
    for call in decisions.approved:
        candidates = [r for r in waiting.get(call.tool_call_id, []) if r.needs_confirmation]
        requirement = next((r for r in candidates if r.member_agent_id == call.invoking_agent), None) or next(
            (r for r in candidates if r.member_agent_id is None),
            None,
        )
        if requirement is None or requirement.tool_execution.tool_name != call.tool_name:
            msg = "Saved approval calls no longer match the pending requirements; retry the request"
            raise RuntimeError(msg)
        require_original_toolkit(toolkit_owners, call)
        approved[(requirement.member_agent_id, call.tool_call_id)] = call
    return TeamDecisions(
        approved=approved,
        denied={key_of(call_id): note for call_id, note in decisions.denied.items()},
        external_results=dict(decisions.external_results),
    )


@dataclass(frozen=True)
class TeamResume:
    """Continue one paused team run as ``leader`` runs; the ``ContinueRun`` the delegation driver uses.

    ``plan`` selects the stored history the leader's resume request replays: the turn's own plan within a turn, the
    configured policy for a saved approval. An approved call must still come from the toolkit its member's build
    offers it from (``TeamSpec.tool_owners``).
    """

    leader: LeaderRun
    records: TerminalRecord[TeamRunRecord]
    plan: ResolvedReplayPlan | None
    user_id: str | None

    @property
    def storage(self) -> RunStorage:
        """Where the paused team run's row lives, the leader's storage."""
        return self.leader.storage

    def events(self, paused: TeamRunRecord, decisions: ResumeDecisions) -> AsyncIterator[RunEvent]:
        """Resume ``paused`` with ``decisions`` routed to the calls the team waits for; the stream ends with ``RunFinished``.

        Routing and the loop's own coverage check run here, before the stream starts, so a resume that no longer fits
        is refused before anything runs. A member run whose own paused call an approval decided reads that approval's
        receipt. The stream carries the members' events too: the driver forwards a parent's events without reading
        them, so member events ride along.
        """
        routed = _route_decisions(paused, decisions, toolkit_owners=self.leader.built.spec.tool_owners())
        decided = {*routed.approved, *routed.denied}
        for requirement in paused.requirements or ():
            key = (requirement.member_agent_id, requirement.tool_execution.tool_call_id)
            if requirement.member_run_id is not None and key in decided:
                add_approval_receipt_reader(
                    requirement.member_run_id,
                    partial(member_call_id, requirement.member_agent_id),
                )
        events = self.leader.resume(paused, routed, plan=self.plan, user_id=self.user_id, records=self.records)
        return cast("AsyncIterator[RunEvent]", events)


def _member_call_event(requirement: RunRequirement, event: ToolStarted | ToolCompleted) -> RunEvent:
    """Put the driver's event about a member's delegation call in the slot of the member whose run made the call.

    The driver reports the delegation call it runs as the parent's own ``ToolStarted`` and ``ToolCompleted``. In a
    team the call belongs to the member whose run paused on it, which the requirement names, since members number
    their calls independently.
    """
    if requirement.member_agent_id is None or requirement.member_run_id is None:
        return event
    member_name = requirement.member_display_name or requirement.member_agent_id
    return cast("RunEvent", MemberEvent(requirement.member_agent_id, member_name, requirement.member_run_id, event))


def drive_team_delegations(
    events: AsyncIterator[TeamRunEvent],
    resume: TeamResume,
    *,
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
) -> AsyncIterator[TeamRunEvent]:
    """Pass the run's events through; when it ends paused on a member's delegation, run the children and continue it.

    The driver sees the team run as a parent, runs a member's delegations as that member, and ``resume`` continues
    the run with the children's results.
    """
    driven = drive_delegation_stream(
        cast("AsyncIterator[RunEvent]", events),
        record_type=TeamRunRecord,
        continue_parent=resume,
        agent_name=team_name,
        run_child=run_delegated_child_response,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        decisions=decisions,
        denial_reasons=denial_reasons,
        approval_calls=approval_calls,
        tag_call_event=_member_call_event,
    )
    return cast("AsyncIterator[TeamRunEvent]", driven)


def continued_team_pause(presentation: TeamStreamPresentation, paused: PausedAttempt) -> PausedAttempt:
    """Render the pending calls into their slots and freeze the document the pause shows.

    Each call waits in the slot ``pending_call_slot`` names, the rule the approval card anchors by too. The approval
    card of a member's own delegation call adds no marker: the member's slot already shows that call pending under
    its own id, and the resume finishes it there. Every other call waits under its own id.
    """
    member_ids = presentation.member_ids
    for requirement in paused.requirements:
        if member_delegation_card(requirement, member_ids):
            continue
        member_id, _ = pending_call_slot(requirement, member_ids)
        if member_id is not None:
            member_id = presentation.resolve_member_id(member_id, requirement.member_display_name)
        presentation.start_tool(requirement.tool_execution.call, member_id=member_id)
    return replace(
        paused,
        response_text=presentation.render_body(finished=False),
        tool_trace=tuple(presentation.tool_trace),
        response_presentation_state=presentation.to_state(),
    )


async def _build_paused_members(team: TeamSpec, paused: TeamRunRecord) -> None:
    """Build the members whose paused runs a saved approval's resume continues, before it checks its decisions.

    The resume checks each member's decisions against the member's tools before anything runs; every other member
    builds on its first delegation, as in a turn.
    """
    paused_members = {requirement.member_agent_id for requirement in paused.requirements or ()}
    for member in team.members:
        if member.id in paused_members:
            await member.ensure_built()


async def _finished(record: TeamRunRecord) -> AsyncIterator[TeamRunEvent]:
    """Yield a stored run as the end of a stream, for the driver to continue."""
    yield RunFinished(record)


@owned_tool_execution(
    enabled=lambda *, config, runtime_paths, **_kwargs: background_tool_jobs_enabled(config, runtime_paths),
)
async def continue_paused_team_run(
    *,
    member_names: tuple[str, ...],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    session_id: str,
    run_id: str,
    user_id: str,
    configured_team_name: str,
    model_name: str,
    decisions: dict[str, bool],
    denial_reasons: dict[str, str | None],
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    member_model_names: Mapping[str, str] | None,
    approval_calls: Sequence[ApprovalCall],
    history_scope: HistoryScope | None,
    prior_tool_trace: Sequence[ToolTraceEntry],
    prior_presentation_state: Mapping[str, object] | None,
    show_tool_calls: bool,
    tool_trace_collector: list[ToolTraceEntry],
    progress: ProgressPublisher | None,
) -> CompletedApprovalRun | PausedAttempt:
    """Rebuild a team and continue its exact persisted paused run.

    ``configured_team_name`` is the entity the approval was saved under: the configured team, or for an ad hoc team
    the member whose account handles it. ``progress``, when given, shows the resumed document live in the reply
    being continued; the terminal delivery stays with the caller.
    """
    if not member_names:
        # Saved approvals name their members; without them no member could run the approved calls.
        msg = "The paused team approval names no members"
        raise RuntimeError(msg)
    # None for an ad hoc team, which compacts and stores as no configured entity.
    team_name = config.configured_team_name(configured_team_name)
    # The entity the run's logs, notices, and delegations name, as its turn named it: the configured team, or the
    # label of an ad hoc team's members once they are known.
    entity_name = team_name or configured_team_name
    # The approval's retained team storage, else the scope its team resolves to before members are rebuilt.
    scope = load_scope_session(
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        scope=history_scope
        or resolve_bound_team_scope(
            member_names=member_names,
            config=config,
            team_name=team_name,
            requester_user_id=execution_identity.requester_id,
        ),
    )
    try:
        set_consumption_storage(scope.storage_factory)
        persisted = _stored_paused_team_run(scope, run_id)
        delegated = has_delegation_state(persisted)
        approved_calls = tuple(call for call in approval_calls if decisions.get(call.tool_call_id))
        async with asyncio.TaskGroup() as recovery:
            required_tool_tasks = {
                name: recovery.create_task(
                    required_approval_tool_names(
                        name,
                        tuple(call for call in approved_calls if call.invoking_agent == name),
                        config=config,
                        runtime_paths=runtime_paths,
                        execution_identity=execution_identity,
                        delegation_depth=0,
                    ),
                )
                for name in member_names
            }
        members = await asyncio.to_thread(
            resolve_exact_team_members,
            list(member_names),
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=True,
            # A pause a resume made saves no member models, so its own resume resolves them by the team rule.
            active_model_names=member_model_names
            if member_model_names is not None
            else resolve_team_turn_models(
                config,
                runtime_paths,
                ModelScope(
                    entity=configured_team_name,
                    room_id=execution_identity.room_id,
                    thread_id=execution_identity.resolved_thread_id,
                ),
                member_names,
                None,
            ).member_model_names,
            required_tool_names={name: task.result() for name, task in required_tool_tasks.items()},
        )
        if team_name is None:
            entity_name = materialized_team_label(members.display_names)
        built = build_team_spec(
            members=members,
            mode=mode,
            config=config,
            runtime_paths=runtime_paths,
            scope_context=scope,
            execution_identity=execution_identity,
            model_name=model_name,
            configured_team_name=team_name,
            timezone=entity_timezone(config, configured_team_name),
        )
        await _build_paused_members(built.spec, persisted)
        native = restore_native_history(built.spec.leader.model, persisted_run=persisted, session=scope.session)
        presentation = TeamStreamPresentation.restore(
            config_names=member_names,
            show_tool_calls=show_tool_calls,
            state=prior_presentation_state,
            tool_trace=prior_tool_trace,
        )
        storage = scope_run_storage(scope)
        attempt = TeamAttempt(
            run_id=run_id,
            members=members,
            team=built.spec,
            presentation=presentation,
            team_name=entity_name,
            runtime_model_name=model_name,
            prepared_history=None,
        )
        plan = configured_replay_plan(history_settings=built.history_settings, estimated_tokens=0)
        leader = LeaderRun(
            built=built,
            storage=storage,
            hooks=leader_loop_hooks(
                built,
                HookTurn(
                    config=config,
                    runtime_paths=runtime_paths,
                    model_name=model_name,
                    storage=storage,
                    replay_plan=plan,
                    prompt_id=None,
                ),
                entity_name=team_name,
            ),
            request_template=with_native_history(ModelRequest(messages=()), native),
            model_name=model_name,
        )
        resume = TeamResume(
            leader=leader,
            records=attempt.records,
            plan=plan,
            user_id=user_id,
        )
        # A run the delegation driver owns resumes through it with the decisions on its children's calls; any
        # other run resumes with the decisions on its own calls, and the driver continues it if it then pauses
        # on a member's delegation.
        events = drive_team_delegations(
            _finished(persisted)
            if delegated
            else resume.events(persisted, approval_decisions(approval_calls, decisions, denial_reasons)),
            resume,
            team_name=entity_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            refresh_scheduler=refresh_scheduler,
            decisions=decisions if delegated else None,
            denial_reasons=denial_reasons if delegated else None,
            approval_calls=approval_calls if delegated else (),
        )
        events = with_resumed_turn_log_context(events, entity_name=entity_name)
        with helper_usage_context(scope):
            await publish_team_presentation(progress, presentation)
            async with closing_async_stream(events):
                async for event in events:
                    if attempt.apply(event):
                        await publish_team_presentation(progress, presentation)
        continued = attempt.terminal_record()
        # Each resumed run's slot shows what the run hands over beyond its pause and its events did not show, such as
        # a failed member run's reason; a run that paused again shows nothing more.
        for run in (continued, *continued.ended_member_runs):
            attempt.show_unshown_handover(run)
        paused = paused_attempt_from_response(continued, toolkit_owners=built.spec.tool_owners())
        if paused is not None:
            return continued_team_pause(presentation, paused)
        if continued.status is not RunStatus.completed:
            # Paused on a call no approval asks about and no delegation answers, as the agent resume fails on it.
            raise pause_without_approval_error(continued, entity_label="team")
        if show_tool_calls:
            tool_trace_collector.extend(presentation.tool_trace)
        # The document already holds what each run wrote before the pause and handed over since, the leader's included,
        # so one that stays empty, every run's text blank, shows the no-reply note, as a new team turn's does.
        response_text = presentation.render_body(finished=True) or (
            format_team_header(members.display_names) + NO_TEAM_RESPONSE
        )
        return CompletedApprovalRun(
            response_text=response_text,
            metadata_content=attempt.metadata_content(
                config,
                runtime_paths=runtime_paths,
                session_id=session_id,
                status=continued.status,
            ),
        )
    finally:
        try:
            await finalize_consumption()
        finally:
            ai_runtime.register_scope_notice_storage(scope_context=scope, entity_name=entity_name)


__all__ = [
    "TeamResume",
    "continue_paused_team_run",
    "continued_team_pause",
    "drive_team_delegations",
]
