# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Team runs and resumes over the agent loop.

A team run is the leader's loop over its own tools plus the delegation tool for the team's mode. Members run as
children of the team run, each ending in a record of its own with ``parent_run_id``, and their events reach the
team's stream tagged with the member. A paused member pauses the team: the team run holds the paused member run in
``member_responses`` and waits on its calls, naming the member, and ``continue_team`` resumes the members first, then
the leader. The team record holds every member run that ended while it ran in ``ended_member_runs``, and the leader's
usage in ``metrics``; ``team_usage`` adds the member runs' usage once.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from mindroom.agent_loop.run import (
    Persist,
    ResumePlan,
    check_approved_tools_offered,
    decide_requirements,
    drive_run,
    fail_unbuilt_run,
    plan_resume,
    resume_planned,
    resumed_record,
    run_agent,
    started_run_fields,
)
from mindroom.agent_loop.tools import PreparedTools, sorted_specs
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.teams.delegate_tools import DELEGATION_TOOLS, answer_delegation, delegate_functions
from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.run import ApprovedCall
    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest, ReasoningDelta, ToolSpec
    from mindroom.llm.runs import AnyRunRecord, RunRequirement
    from mindroom.llm.usage import RunMetrics
    from mindroom.teams.delegate_tools import MemberRunner
    from mindroom.teams.spec import MemberBuild, TeamMember, TeamSpec

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MemberEvent:
    """One of a member run's events, tagged with the member and its run."""

    member_id: str
    member_name: str
    run_id: str
    event: TextDelta | ReasoningDelta | ToolStarted | ToolCompleted


type TeamRunEvent = RunEvent | MemberEvent
type Emit = Callable[[TeamRunEvent], None]
# One call a paused team waits for: the member whose run issued it, or None for the leader's own, and the call id.
# Members' providers number their calls independently, so a call id alone does not name a call of the team.
type TeamCallKey = tuple[str | None, str]


def run_team(
    spec: TeamSpec,
    run_input: Sequence[Message],
    *,
    ctx: RunContext,
    hooks: LoopHooks,
    persist: Persist[AnyRunRecord],
    request_template: ModelRequest,
) -> AsyncIterator[TeamRunEvent]:
    """Run one team turn: the leader's system messages, then ``run_input`` as the history subsystem built it.

    The stream yields the leader's events, every member's events as ``MemberEvent``, and finally ``RunFinished`` with
    the ``COMPLETED`` or ``PAUSED`` team record after ``persist`` stored it; ``persist`` also receives every member
    record as its run ends. The team record ends as ``run_agent``'s does on cancellation and errors, and cancelling
    the stream cancels the members with the leader.
    """
    leader = spec.leader
    record = TeamRunRecord(**started_run_fields(ctx, leader.model, metadata=ctx.metadata), team_id=leader.id)
    messages = [*leader.system_messages, *run_input]
    return _drive_team(
        spec,
        record,
        messages,
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
    )


def continue_team(
    spec: TeamSpec,
    paused: TeamRunRecord,
    messages: list[Message],
    *,
    decisions: TeamDecisions,
    context: Callable[[str], RunContext],
    hooks: LoopHooks,
    persist: Persist[AnyRunRecord],
    request_template: ModelRequest,
) -> AsyncIterator[TeamRunEvent]:
    """Resume a paused team run: its paused members with the decisions on their calls, then the leader.

    ``messages`` is the leader's resume request, built from ``paused`` as ``build_resume_request`` builds it, and
    ``context`` returns the context of the run with a given id, which the resume calls with ``paused``'s.
    Every member whose paused run the resume continues must be built (``TeamMember.ensure_built``): its run in this
    team built it, or a saved approval's resume builds it first. Every call the team waits for must be covered
    exactly, as ``resume_agent`` requires, and every decision must name a waiting call, else ``ValueError`` here
    before anything runs.
    A member whose resumed run ends answers the delegation call that waited on it (``answer_delegation``), even while
    another member still waits, so the leader reads every member's result once none waits; a member that pauses
    again keeps the team paused, waiting on its new calls, and the leader sends no request.
    """
    # A resume that fails or is cancelled stores this record over the paused team row on purpose, as an agent's does
    # (``_store_interrupted`` in ``agent_loop/run.py``): the resume ends its approval anyway.
    record = resumed_record(paused)
    # The members resume first, each from its own record, so the leader's record waits only on its own calls.
    record.member_responses = []
    plans = _plan_member_resumes(spec, paused, decisions)
    leader = decisions.take(None, {r.tool_execution.tool_call_id for r in record.requirements})
    decided = decide_requirements(
        record,
        approved=leader.approved,
        denied=leader.denied,
        external_results=leader.external_results,
    )
    check_approved_tools_offered(spec.leader, leader.approved)
    decisions.require_consumed()

    async def entry(members: _MemberRuns) -> None:
        for plan in plans:
            resumed = await members.resume(plan)
            if resumed.status is RunStatus.paused:
                record.member_responses.append(resumed)
            else:
                _answer_delegation(record, messages, plan.member, resumed)

    return _drive_team(
        spec,
        record,
        messages,
        ctx=context(record.run_id),
        hooks=hooks,
        persist=persist,
        template=request_template,
        entry=entry,
        resumed=decided,
    )


def team_usage_runs(record: TeamRunRecord) -> list[tuple[AnyRunRecord, RunMetrics]]:
    """Return the runs whose own metrics make up the turn's usage, with those metrics: the leader's, then each member's.

    The team record's own metrics cover only the leader's requests; each member run that ended
    (``ended_member_runs``) carries its usage on its record, so nothing is counted twice. A run that reported no
    usage is left out.
    """
    return [(run, run.metrics) for run in (record, *record.ended_member_runs) if run.metrics is not None]


def team_usage(record: TeamRunRecord) -> RunMetrics | None:
    """Return the turn's usage, the sum over ``team_usage_runs``, within the leader's duration.

    Members run inside the leader's window, so summing durations would double-count wall clock; token sums are
    disjoint.
    """
    metrics = [run_metrics for _, run_metrics in team_usage_runs(record)]
    if not metrics:
        return None
    total = sum(metrics[1:], metrics[0])
    if len(metrics) > 1 and record.metrics is not None:
        total.duration = record.metrics.duration
    return total


@dataclass(frozen=True, slots=True)
class _Decided:
    """The decisions that fall to one run of the team: the leader's own or one member's."""

    approved: tuple[ApprovedCall, ...]
    denied: dict[str, str | None]
    external_results: dict[str, str]


@dataclass(slots=True)
class TeamDecisions:
    """One resume's decisions by ``TeamCallKey``, handed out run by run so a stray decision is caught.

    ``continue_team`` records the calls it takes, so an instance serves one resume.
    """

    approved: Mapping[TeamCallKey, ApprovedCall]
    denied: Mapping[TeamCallKey, str | None]
    external_results: Mapping[TeamCallKey, str]
    consumed: set[TeamCallKey] = field(default_factory=set, init=False)

    def take(self, member_id: str | None, call_ids: set[str]) -> _Decided:
        """Return the decisions on ``member_id``'s calls among ``call_ids`` (None names the leader's own)."""
        keys = {(member_id, call_id) for call_id in call_ids}
        self.consumed |= keys
        return _Decided(
            approved=tuple(call for key, call in self.approved.items() if key in keys),
            denied={key[1]: note for key, note in self.denied.items() if key in keys},
            external_results={key[1]: result for key, result in self.external_results.items() if key in keys},
        )

    def require_consumed(self) -> None:
        """Refuse a decision that names no call the team waits for."""
        stray = ({*self.approved, *self.denied, *self.external_results}) - self.consumed
        if stray:
            named = sorted(stray, key=lambda key: (key[0] or "", key[1]))
            msg = f"Decisions name calls the team does not wait for: {named!r}; retry the request"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class _MemberResume:
    member: TeamMember
    build: MemberBuild
    paused: RunRecord
    plan: ResumePlan


def _plan_member_resumes(spec: TeamSpec, paused: TeamRunRecord, decisions: TeamDecisions) -> list[_MemberResume]:
    """Plan one resume per paused member run, in ``member_responses`` order, checking every member's decisions first."""
    plans: list[_MemberResume] = []
    for member_record in paused.member_responses:
        member_id, run_id = member_record.agent_id, member_record.run_id
        member = next((member for member in spec.members if member.id == member_id), None)
        if member is None:
            msg = (
                f"Paused member run {run_id!r} of team run {paused.run_id!r} belongs to "
                f"{member_id!r}, which is not a member of team {spec.leader.id!r}"
            )
            raise ValueError(msg)
        build = member.built
        assert build is not None, f"paused member {member_id!r} of team run {paused.run_id!r} resumes unbuilt"
        decided = decisions.take(member_id, {item.tool_execution.tool_call_id for item in member_record.requirements})
        plan = plan_resume(
            build.spec,
            member_record,
            approved=decided.approved,
            denied=decided.denied,
            external_results=decided.external_results,
        )
        plans.append(_MemberResume(member, build, member_record, plan))
    return plans


def _answer_delegation(
    record: TeamRunRecord,
    messages: list[Message],
    member: TeamMember,
    resumed: RunRecord,
) -> None:
    """Answer the delegation call that waited on ``member``'s paused run, which ended as ``resumed``.

    The answer lands on the run's tool entry and on the leader's request, so the leader reads it where it read the
    pause notice, and a team that pauses again stores it. A provider may reuse a call id in a later request, so the
    call's message is the latest under its id.
    """
    call = answer_delegation(record.tools or (), member, resumed)
    if call is None:
        return
    for index in reversed(range(len(messages))):
        message = messages[index]
        if message.role == "tool" and message.tool_call_id == call.tool_call_id:
            messages[index] = replace(message, content=call.result)
            return


def _leader_spec(spec: TeamSpec, record: TeamRunRecord, run_member: MemberRunner) -> AgentSpec:
    """Return the leader as the loop runs it: the team's own tools, then the delegation tool bound to this run."""
    leader = spec.leader
    delegation = delegate_functions(spec.members, spec.mode, record=record, run_member=run_member)
    return replace(leader, toolkits=(*leader.toolkits, delegation))


def leader_tool_specs(spec: TeamSpec) -> tuple[ToolSpec, ...]:
    """Return the tool definitions every leader request offers, in request order: its own tools and the delegation tool.

    The delegation tool's definition is the one ``DELEGATION_TOOLS`` declares for the team's mode, whichever run binds
    it.
    """
    return sorted_specs((*PreparedTools(functions=spec.leader.functions).specs(), DELEGATION_TOOLS[spec.mode]))


@dataclass(frozen=True, slots=True)
class _MemberRuns:
    """Run and resume the members of one team run, forwarding their events tagged with the member."""

    record: TeamRunRecord
    ctx: RunContext
    emit: Emit
    persist: Persist[AnyRunRecord]

    async def run(self, member: TeamMember, task: str) -> RunRecord:
        """Run ``member`` on the leader's task as a child of the team run; the task text is its whole input.

        The member's first run builds it (``TeamMember.ensure_built``). A member that cannot be built ends the run as
        failed with why (``fail_unbuilt_run``), so the leader reads the reason as it reads a failed run's, and the team
        run goes on. The member's tools read the team run's metadata, which only the team's record stores.
        """
        ctx = self._member_context(str(uuid4()))
        run_input = [Message(role="user", content=task)]
        try:
            build = await member.ensure_built()
        except Exception as error:
            logger.warning(
                "team_member_build_failed",
                team_id=self.record.team_id,
                member_id=member.id,
                run_id=ctx.run_id,
                exc_info=True,
            )
            return await fail_unbuilt_run(
                ctx,
                run_input,
                agent_id=member.id,
                agent_display_name=member.name,
                error=error,
                persist=self._persist,
            )
        stream = run_agent(
            build.spec,
            run_input,
            ctx=ctx,
            hooks=build.hooks(ctx.run_id),
            persist=self._persist,
            request_template=build.request_template,
        )
        return await self._finish(member, ctx.run_id, stream)

    async def resume(self, planned: _MemberResume) -> RunRecord:
        """Resume a member's paused run, under that run's hooks, from its stored messages, which hold no history."""
        build, paused = planned.build, planned.paused
        stream = resume_planned(
            build.spec,
            planned.plan,
            build_resume_request(paused=paused.messages, summary=None, history=[]),
            context=self._member_context,
            hooks=build.hooks(paused.run_id),
            persist=self._persist,
            request_template=build.request_template,
        )
        return await self._finish(planned.member, paused.run_id, stream)

    def _member_context(self, run_id: str) -> RunContext:
        """Return a member run's context: the team run's, so its tools share the team's state, under its own run id."""
        return replace(self.ctx, run_id=run_id, parent_run_id=self.record.run_id)

    async def _persist(self, record: RunRecord) -> None:
        # The team keeps member runs in the order they first ended; a member resumed within the run ends twice, and
        # its later record replaces the paused one where it stood.
        run_ids = [run.run_id for run in self.record.ended_member_runs]
        if record.run_id in run_ids:
            self.record.ended_member_runs[run_ids.index(record.run_id)] = record
        else:
            self.record.ended_member_runs.append(record)
        await self.persist(record)

    async def _finish(self, member: TeamMember, run_id: str, stream: AsyncIterator[RunEvent]) -> RunRecord:
        """Forward the member's events and return the terminal record its loop stored however it ended.

        A member whose run fails is stored ``ERROR`` by its loop and still reaches the leader, with the reason its
        record carries (``handover_content``), so one member's failure does not fail the team run; only cancellation
        propagates.
        """
        try:
            async with closing_async_stream(stream):
                async for event in stream:
                    # A team reply shows no member's live tool output.
                    if not isinstance(event, RunFinished | ToolProgress):
                        self.emit(MemberEvent(member.id, member.name, run_id, event))
        except Exception as error:
            logger.warning(
                "team_member_run_failed",
                team_id=self.record.team_id,
                member_id=member.id,
                run_id=run_id,
                error=str(error),
            )
        return next(run for run in self.record.ended_member_runs if run.run_id == run_id)


async def _drive_team(
    spec: TeamSpec,
    record: TeamRunRecord,
    messages: list[Message],
    *,
    ctx: RunContext,
    hooks: LoopHooks,
    persist: Persist[AnyRunRecord],
    template: ModelRequest,
    entry: Callable[[_MemberRuns], Awaitable[None]] | None = None,
    resumed: Sequence[RunRequirement] | None = None,
) -> AsyncIterator[TeamRunEvent]:
    """Stream the leader's loop over ``messages`` and its members' runs, owning the team run's cancellation.

    ``entry`` is the work the run does with its members before the loop, and ``resumed`` the leader's decided calls.
    """

    async def produce(emit: Emit) -> None:
        members = _MemberRuns(record, ctx, emit, persist)
        leader = _leader_spec(spec, record, members.run)
        events = drive_run(
            leader,
            record,
            messages,
            ctx=ctx,
            hooks=hooks,
            persist=persist,
            template=template,
            entry=None if entry is None else lambda: entry(members),
            resumed=resumed,
        )
        async with closing_async_stream(events):
            async for event in events:
                emit(event)

    produced = callback_event_stream(produce)
    async with closing_async_stream(produced):
        async for item in produced:
            # The producer's own result, None, comes last; everything before it is an event.
            if item is not None:
                yield cast("TeamRunEvent", item)


__all__ = [
    "Emit",
    "MemberEvent",
    "TeamCallKey",
    "TeamDecisions",
    "TeamRunEvent",
    "continue_team",
    "leader_tool_specs",
    "run_team",
    "team_usage",
    "team_usage_runs",
]
