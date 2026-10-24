"""Resuming a paused team run: within a turn, after a member's delegations, and from a saved approval.

``TeamResume`` continues a paused run under a turn's hooks and storage, with the decisions routed to the member or
leader calls the team waits for; ``drive_team_delegations`` runs the children of a member's delegations and
continues the run with their results. ``continue_paused_team_run`` rebuilds the team from a saved approval and
resumes its stored run through both.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom import ai_runtime
from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.ai import run_delegated_child_response
from mindroom.approval.receipt_readers import add_approval_receipt_reader
from mindroom.approval.resume import approval_decisions, forward_resume, with_resumed_turn_log_context
from mindroom.approval.tool_owners import require_original_toolkit, required_approval_tool_names
from mindroom.delegation.execution import drive_delegation_stream, has_delegation_state
from mindroom.delegation.state import member_delegation_card, pending_call_slot
from mindroom.helper_usage import helper_usage_context
from mindroom.history.native import restore_native_history, with_native_history
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import build_resume_request, replayed_history
from mindroom.history.session_context import open_resolved_scope_session_context
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope
from mindroom.llm.events import RunFinished
from mindroom.llm.provider import ModelRequest
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.loop_hooks import HookTurn, leader_loop_hooks
from mindroom.response_turn import CompletedApprovalRun, paused_attempt_from_response
from mindroom.teams.attempt import TeamAttempt
from mindroom.teams.members import (
    build_team_spec,
    materialize_exact_team_members,
    materialized_team_label,
    team_label,
)
from mindroom.teams.presentation import (
    TeamStreamPresentation,
    finished_run_text,
    format_team_header,
    publish_team_presentation,
    team_response_text,
)
from mindroom.teams.runtime import MemberEvent, continue_team
from mindroom.teams.storage import (
    configured_team,
    persist_into,
    scope_storage,
    team_history_settings,
)
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.run import ApprovedCall
    from mindroom.approval.resume import ResumeDecisions, ResumeEvents
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import ResolvedHistorySettings, ResolvedReplayPlan
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.events import RunEvent, ToolCompleted, ToolStarted
    from mindroom.llm.runs import AnyRunRecord, RunRecord, RunRequirement
    from mindroom.response_turn import PausedAttempt
    from mindroom.streaming import ProgressPublisher
    from mindroom.teams.runtime import TeamCallKey, TeamRunEvent
    from mindroom.teams.spec import TeamMode, TeamSpec
    from mindroom.teams.storage import TeamRunRecords, TeamStorage
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _approval_history_scope(
    member_names: tuple[str, ...],
    team_name: str | None,
    config: Config,
    execution_identity: ToolExecutionIdentity,
    retained_scope: HistoryScope | None,
) -> HistoryScope | None:
    """Resolve retained team storage before reconstructing member executables; ``team_name`` is None for an ad hoc team."""
    if retained_scope is not None:
        return retained_scope
    scope_id = (
        team_name
        if team_name is not None
        else ad_hoc_team_scope_id(member_names, config.agents, requester_user_id=execution_identity.requester_id)
    )
    return HistoryScope(kind="team", scope_id=scope_id) if scope_id is not None else None


def _stored_paused_team_run(scope: ScopeSessionContext, run_id: str) -> TeamRunRecord:
    """Return the paused team run the approval names, from the loaded session or the store.

    A run the store holds for another session of the scope than the one ``scope`` was opened for is refused, as the
    agent path refuses it.
    """
    persisted: AnyRunRecord | None = None
    if scope.session is not None:
        persisted = next((run for run in scope.session.runs if run.run_id == run_id), None)
    if persisted is None:
        persisted = scope.storage.get_run(run_id)
    if (
        not isinstance(persisted, TeamRunRecord)
        or persisted.session_id != scope.session_id
        or persisted.status is not RunStatus.paused
    ):
        msg = f"Paused team run {run_id!r} is no longer available"
        raise RuntimeError(msg)
    return persisted


@dataclass(frozen=True)
class _RoutedDecisions:
    """The resume's decisions routed to the calls the paused team waits for, by ``TeamCallKey``."""

    approved: dict[TeamCallKey, ApprovedCall]
    denied: dict[TeamCallKey, str | None]
    external_results: dict[TeamCallKey, str]


def _route_decisions(
    persisted: TeamRunRecord,
    decisions: ResumeDecisions,
    *,
    toolkit_owners: Mapping[tuple[str, str], str],
) -> _RoutedDecisions:
    """Route each decision to the waiting member or leader call it names, checking an approved call's toolkit owner.

    An approved call is matched by the member the approval was shown for, and must still come from the toolkit the
    approval recorded, so a removed or replaced tool cannot run under an old approval. A denial names its call by id
    alone, and a call id two members share is refused rather than guessed at; a delegation's result names its member
    and call, which the resume checks against the calls the team waits for.
    """
    waiting: dict[str | None, list[RunRequirement]] = {}
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
    return _RoutedDecisions(
        approved=approved,
        denied={key_of(call_id): note for call_id, note in decisions.denied.items()},
        external_results=dict(decisions.external_results),
    )


@dataclass(frozen=True)
class TeamResume:
    """Continue one paused team run under a turn's hooks and storage; the ``ContinueRun`` the delegation driver calls.

    ``plan`` selects the stored history the leader's resume request replays: the turn's own plan within a turn, the
    configured policy for a saved approval.
    """

    spec: TeamSpec
    storage: TeamStorage
    records: TeamRunRecords
    hooks: LoopHooks
    request_template: ModelRequest
    settings: ResolvedHistorySettings
    plan: ResolvedReplayPlan | None
    user_id: str | None
    toolkit_owners: Mapping[tuple[str, str], str]

    def events(self, paused: TeamRunRecord, decisions: ResumeDecisions) -> AsyncIterator[TeamRunEvent]:
        """Resume ``paused`` with ``decisions`` routed to the calls the team waits for; the stream ends with ``RunFinished``.

        Routing and the loop's own coverage check run here, before the stream starts, so a resume that no longer fits
        is refused before anything runs. A member run whose own paused call an approval decided reads that approval's
        receipt.
        """
        routed = _route_decisions(paused, decisions, toolkit_owners=self.toolkit_owners)
        decided = {*routed.approved, *routed.denied}
        for requirement in paused.requirements or ():
            key = (requirement.member_agent_id, requirement.tool_execution.tool_call_id)
            if requirement.member_run_id is not None and key in decided:
                add_approval_receipt_reader(requirement.member_run_id)
        session = self.storage.session
        # The leader's resume request: its stored system message, the summary and history ``plan`` replays, the rest.
        history, summary = replayed_history(session, self.storage.scope, self.settings, self.plan)
        return continue_team(
            self.spec,
            paused,
            build_resume_request(paused=paused.messages, summary=summary, history=history),
            approved=routed.approved,
            denied=routed.denied,
            external_results=routed.external_results,
            ctx=RunContext(
                run_id=paused.run_id,
                session_id=session.session_id,
                user_id=self.user_id,
                metadata=deepcopy(paused.metadata),
                session=session,
                history_scope=self.storage.scope,
            ),
            hooks=self.hooks,
            persist=persist_into(self.records, self.storage),
            request_template=self.request_template,
        )

    async def __call__(
        self,
        paused: TeamRunRecord,
        decisions: ResumeDecisions,
        on_event: ResumeEvents | None,
    ) -> TeamRunRecord:
        """Resume ``paused`` with its delegations' results, forwarding its events, and return its terminal record.

        The driver forwards a parent's events without reading them, so member events ride along.
        """
        events = cast("AsyncIterator[RunEvent]", self.events(paused, decisions))
        return await forward_resume(events, on_event, record_type=TeamRunRecord)


def _member_call_event(requirement: RunRequirement, event: ToolStarted | ToolCompleted) -> RunEvent:
    """Put the driver's event about a member's delegation call in the slot of the member whose run made the call.

    The driver reports the delegation call it runs as the parent's own ``ToolStarted`` and ``ToolCompleted``. In a
    team the call belongs to the member whose run paused on it, which the requirement names, since members number
    their calls independently.
    """
    if requirement.member_agent_id is None or requirement.member_run_id is None:
        return event
    member_name = requirement.member_agent_name or requirement.member_agent_id
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
        store=resume.storage.store,
        session=resume.storage.session,
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


def _waiting_slot(presentation: TeamStreamPresentation, requirement: RunRequirement) -> tuple[str, str | None]:
    """Return the slot that shows the call ``requirement`` waits on and the call's id there, by ``pending_call_slot``."""
    member_id, call_id = pending_call_slot(requirement, presentation.config_names_by_id.values())
    if member_id is None:
        return "team", call_id
    return f"agent:{presentation.resolve_member_id(member_id, requirement.member_agent_name)}", call_id


def continued_team_pause(presentation: TeamStreamPresentation, paused: PausedAttempt) -> PausedAttempt:
    """Render the pending calls into their slots and freeze the document the pause shows.

    The approval card of a member's own delegation call adds no marker: the member's slot already shows that call
    pending under its own id, and the resume finishes it there.
    """
    member_names = presentation.config_names_by_id.values()
    for requirement in paused.requirements:
        if member_delegation_card(requirement, member_names):
            continue
        scope, _ = _waiting_slot(presentation, requirement)
        presentation.start_tool(scope, requirement.tool_execution)
    return replace(
        paused,
        response_text=presentation.render_body(),
        tool_trace=tuple(presentation.tool_trace),
        response_presentation_state=presentation.to_state(),
    )


def blocking_team_pause(
    paused: PausedAttempt,
    record: TeamRunRecord,
    members: Sequence[RunRecord],
    *,
    config_names: Sequence[str],
    display_names: Sequence[str],
    show_tool_calls: bool,
) -> PausedAttempt:
    """Render a blocking team pause from its runs' records into the document the resume continues, as Agno's did.

    Each run's text fills its slot first, Agno's paused notices included: the leader's is the consensus, and each
    member run that ended adds its own to its member's slot. Every run's tool calls follow in their slots, still
    pending where the pause waits on them; a member's own delegation card waits on that member's call.
    """
    presentation = TeamStreamPresentation.new(config_names, display_names, show_tool_calls=show_tool_calls)
    waiting = {_waiting_slot(presentation, requirement) for requirement in paused.requirements}
    presentation.append_consensus(finished_run_text(record))
    calls = [("team", tool) for tool in record.tools or ()]
    for member in members:
        member_id = presentation.resolve_member_id(member.agent_id, member.agent_name)
        presentation.append_member(member_id, finished_run_text(member))
        calls.extend((f"agent:{member_id}", tool) for tool in member.tools or ())
    for scope, tool in calls:
        presentation.start_tool(scope, tool)
        if (scope, tool.tool_call_id) not in waiting:
            presentation.complete_tool(scope, tool)
    return continued_team_pause(presentation, paused)


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
    member_model_names: Mapping[str, str] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
    history_scope: HistoryScope | None = None,
    prior_tool_trace: Sequence[ToolTraceEntry] = (),
    prior_presentation_state: Mapping[str, object] | None = None,
    show_tool_calls: bool = True,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
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
    team_name = configured_team(config, configured_team_name)
    # The entity the run's logs, notices, and delegations name, as its turn named it: the configured team, or the
    # label of an ad hoc team's members once they are known.
    entity_name = team_name or configured_team_name
    with open_resolved_scope_session_context(
        agent_name=min(member_names),
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        scope=_approval_history_scope(member_names, team_name, config, execution_identity, history_scope),
    ) as scope:
        try:
            # Named members always resolve a team scope, and the approval names its session.
            assert scope is not None
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
                        ),
                    )
                    for name in member_names
                }
            members = await asyncio.to_thread(
                materialize_exact_team_members,
                list(member_names),
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                refresh_scheduler=refresh_scheduler,
                supports_native_tool_approval=True,
                active_model_names=member_model_names,
                required_tool_names={name: task.result() for name, task in required_tool_tasks.items()},
            )
            if team_name is None:
                entity_name = materialized_team_label(members.display_names)
            built = build_team_spec(
                members=members,
                mode=mode,
                config=config,
                runtime_paths=runtime_paths,
                team_display_name=team_label(member_names),
                scope_context=scope,
                execution_identity=execution_identity,
                model_name=model_name,
                configured_team_name=team_name,
            )
            toolkit_owners = tool_owners_by_agent(*members.builds)
            native = restore_native_history(built.spec.model, persisted_run=persisted, session=scope.session)
            presentation = TeamStreamPresentation.restore(
                config_names=member_names,
                show_tool_calls=show_tool_calls,
                state=prior_presentation_state,
                tool_trace=prior_tool_trace,
            )
            storage = scope_storage(scope)
            attempt = TeamAttempt(
                run_id=run_id,
                members=members,
                presentation=presentation,
                stream=True,
                team_name=entity_name,
                runtime_model_name=model_name,
                prepared_history=None,
            )
            settings = team_history_settings(config, team_name)
            plan = configured_replay_plan(history_settings=settings, estimated_tokens=0)
            resume = TeamResume(
                spec=built.spec,
                storage=storage,
                records=attempt.records,
                hooks=leader_loop_hooks(
                    HookTurn(
                        config=config,
                        runtime_paths=runtime_paths,
                        model_name=model_name,
                        store=storage.store,
                        session=storage.session,
                        scope=storage.scope,
                        replay_plan=plan,
                        prompt_id=None,
                    ),
                    entity_name=team_name,
                    scope_id=built.spec.id,
                    model=built.spec.model,
                    history_settings=settings,
                    tool_middleware=built.leader_middleware,
                    jobs=built.leader_jobs,
                ),
                request_template=with_native_history(ModelRequest(messages=()), native),
                settings=settings,
                plan=plan,
                user_id=user_id,
                toolkit_owners=toolkit_owners,
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
            continued = attempt.records.terminal(entity_name)
            # Each resumed run's slot shows what the run hands over and its events did not show, as Agno's resume added
            # their content: a paused notice, or a failed member run's reason. A run without content adds nothing,
            # where Agno's resume repeated its messages' text from before the pause.
            for run in (continued, *continued.ended_member_runs):
                attempt.show_unshown_handover(run)
            paused = paused_attempt_from_response(
                continued,
                fallback_session_id=session_id,
                fallback_run_id=run_id,
                toolkit_owners=toolkit_owners,
            )
            if paused is not None:
                return continued_team_pause(presentation, paused)
            if continued.status is not RunStatus.completed:
                raise RuntimeError(str(continued.handover_content or "Team continuation did not complete"))
            if tool_trace_collector is not None and show_tool_calls:
                tool_trace_collector.extend(presentation.tool_trace)
            # A resume whose document stays empty, every run's text blank, renders its finished runs, as Agno's did.
            response_text = presentation.render_body() or (
                format_team_header(members.display_names) + team_response_text(continued)
            )
            return CompletedApprovalRun(
                response_text=response_text,
                metadata_content=attempt.metadata_content(config, session_id=session_id, status=continued.status),
            )
        finally:
            try:
                await finalize_consumption()
            finally:
                ai_runtime.register_scope_notice_storage(scope_context=scope, entity_name=entity_name)


__all__ = [
    "TeamResume",
    "blocking_team_pause",
    "continue_paused_team_run",
    "continued_team_pause",
    "drive_team_delegations",
]
