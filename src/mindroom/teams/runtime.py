# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Team runs and resumes over the owned loop.

A team run is the leader's loop over its own tools plus the delegation tool for the team's mode. Members run as
children of the team run, each ending in a record of its own with ``parent_run_id``, and their events reach the
team's stream tagged with the member. A paused member pauses the team: its requirements sit on the team run naming
the member, and ``continue_team`` resumes the members first, then the leader. The team record holds paused members in
``member_responses``, every member run that ended while it ran in ``ended_member_runs``, and the leader's usage in
``metrics``; ``team_usage`` adds the member runs' usage once.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, LoopHooks
from mindroom.agent_loop.run import (
    Persist,
    ResumePlan,
    decide_requirements,
    drive_run,
    plan_resume,
    prepare_resume_tools,
    resume_planned,
    resumed_record,
    run_agent,
)
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.events import ContentDelta, RequestCompleted, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ReasoningDelta
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger
from mindroom.teams.delegate_tools import delegate_functions, propagate_member_pause, waited_on_member
from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.run import ApprovedCall
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest, ToolSpec
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.teams.delegate_tools import MemberRunner
    from mindroom.teams.spec import TeamMember, TeamSpec

logger = get_logger(__name__)

# Agno's texts for the leader after its members' approvals were resolved.
MEMBER_RESULTS_HEADING = "Member results after human-in-the-loop resolution:"
MEMBER_RESULT_LINE = "[{name}]: {content}"
MEMBER_TASK_COMPLETED = "Task completed"

type MemberLoopEvent = ContentDelta | ReasoningDelta | ToolStarted | ToolCompleted | RequestCompleted


@dataclass(frozen=True, slots=True)
class MemberEvent:
    """One of a member run's events, tagged with the member and its run; ``ToolCompleted`` names the team run as parent."""

    member_id: str
    member_name: str
    run_id: str
    event: MemberLoopEvent


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
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist[AnyRunRecord] | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[TeamRunEvent]:
    """Run one team turn: the leader's system message, then ``run_input`` as the history subsystem built it.

    The stream yields the leader's events, every member's events as ``MemberEvent``, and finally ``RunFinished`` with
    the ``COMPLETED`` or ``PAUSED`` team record after ``persist`` stored it; ``persist`` also receives every member
    record as its run ends. The team record ends as ``run_agent``'s does on cancellation and errors, and cancelling
    the stream cancels the members with the leader.
    """
    identity = spec.model.identity
    record = TeamRunRecord(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        model=identity.id,
        model_provider=identity.provider,
        metrics=RunMetrics(),
        metadata=ctx.metadata,
        team_id=spec.id,
        team_name=spec.name,
    )
    messages = [spec.system_message, *run_input]

    async def produce(emit: Emit) -> None:
        stream = request_template.stream
        members = _MemberRuns(spec, record, ctx, emit, persist, stream=stream)
        leader = _leader_spec(spec, record, members.run, stream=stream)
        await _forward(
            drive_run(
                leader,
                record,
                messages,
                prepare_run_tools(leader),
                ctx=ctx,
                hooks=hooks,
                persist=persist,
                template=request_template,
                pause_content=team_paused_content,
            ),
            emit,
        )

    return _team_stream(produce)


def continue_team(
    spec: TeamSpec,
    paused: TeamRunRecord,
    messages: list[Message],
    *,
    approved: Mapping[TeamCallKey, ApprovedCall] = {},
    denied: Mapping[TeamCallKey, str | None] = {},
    external_results: Mapping[TeamCallKey, str] = {},
    ctx: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist[AnyRunRecord] | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[TeamRunEvent]:
    """Resume a paused team run: its paused members with the decisions on their calls, then the leader.

    ``messages`` is the leader's resume request, built from ``paused`` as ``build_resume_request`` builds it.
    Decisions are keyed by ``TeamCallKey``: the member whose run waits for the call, None for the leader's own
    calls. Every call the team waits for must be covered exactly, as ``resume_agent`` requires, and every decision
    must name a waiting call, else ``ValueError`` here before anything runs.
    A member that completes hands the leader its text in Agno's member-results message, which replaces every
    delegation result that waited on a member; a member that pauses again keeps the team paused, with its new
    requirements on the team run, and the leader sends no request.
    """
    record = resumed_record(paused)
    record.member_responses = []
    # Agno's continuation started the leader's text afresh; the paused notice is not part of the answer.
    record.content = None
    member_requirements = [r for r in record.requirements or () if r.member_agent_id is not None]
    record.requirements = [r for r in record.requirements or () if r.member_agent_id is None]
    decisions = _TeamDecisions(approved, denied, external_results)
    plans = _plan_member_resumes(spec, paused, member_requirements, decisions)
    leader = decisions.take(None, {r.tool_execution.tool_call_id for r in record.requirements or ()})
    decided = decide_requirements(
        record,
        approved=leader.approved,
        denied=leader.denied,
        external_results=leader.external_results,
    )
    prepare_resume_tools(_base_leader_spec(spec), leader.approved)
    decisions.require_consumed()

    async def produce(emit: Emit) -> None:
        stream = request_template.stream
        members = _MemberRuns(spec, record, ctx, emit, persist, stream=stream)
        leader = _leader_spec(spec, record, members.run, stream=stream)
        prepared = prepare_run_tools(leader)

        async def entry() -> None:
            results: list[str] = []
            for plan in plans:
                resumed = await members.resume(plan.member, plan.paused, plan.plan)
                if resumed.status is RunStatus.paused:
                    propagate_member_pause(record, plan.member, resumed)
                else:
                    content = resumed.handover_content or MEMBER_TASK_COMPLETED
                    results.append(MEMBER_RESULT_LINE.format(name=plan.member.spec.name, content=content))
            if plans and not any(not r.is_resolved() for r in record.requirements or ()):
                _answer_delegations(record, messages, [plan.member for plan in plans], results)

        await _forward(
            drive_run(
                leader,
                record,
                messages,
                prepared,
                ctx=ctx,
                hooks=hooks,
                persist=persist,
                template=request_template,
                entry=entry,
                resumed=decided,
                pause_content=team_paused_content,
            ),
            emit,
        )

    return _team_stream(produce)


def team_paused_content(record: TeamRunRecord) -> str:
    """Return Agno's text for a team run paused without text of its own: who waits for which confirmation.

    The only externally executed calls are the silent delegation calls, so a pause on delegations alone has no text.
    """
    parts: list[str] = []
    for requirement in record.requirements or ():
        if requirement.needs_confirmation:
            member = requirement.member_agent_name or "team"
            name = requirement.tool_execution.tool_name or "unknown"
            parts.append(f"- {member}: {name} requires confirmation")
    if not parts:
        return ""
    return "Team run paused. The following require input:\n" + "\n".join(parts)


def _keep_ended_member_run(ended: list[RunRecord], record: RunRecord) -> None:
    """Keep a member run's terminal ``record`` in ``ended``, the member runs in the order they first ended.

    A member resumed within the run ends twice; its later record replaces the paused one where it stood, as Agno
    continued the paused member's output in place.
    """
    run_ids = [member.run_id for member in ended]
    if record.run_id in run_ids:
        ended[run_ids.index(record.run_id)] = record
    else:
        ended.append(record)


def team_usage_runs(record: TeamRunRecord, members: Sequence[RunRecord]) -> list[tuple[AnyRunRecord, RunMetrics]]:
    """Return the runs whose own metrics make up the turn's usage, with those metrics: the leader's, then each member's.

    The team record's own metrics cover only the leader's requests; each member's usage is on its record, so
    nothing is counted twice. A run that reported no usage is left out.
    """
    return [(run, run.metrics) for run in (record, *members) if run.metrics is not None]


def team_usage(record: TeamRunRecord, members: Sequence[RunRecord]) -> RunMetrics | None:
    """Return the turn's usage, the sum over ``team_usage_runs``, within the leader's duration.

    Members run inside the leader's window, so summing durations would double-count wall clock; token and cost sums
    are disjoint.
    """
    metrics = [run_metrics for _, run_metrics in team_usage_runs(record, members)]
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
class _TeamDecisions:
    """The resume's decisions by ``TeamCallKey``, handed out run by run so a stray decision is caught."""

    approved: Mapping[TeamCallKey, ApprovedCall]
    denied: Mapping[TeamCallKey, str | None]
    external_results: Mapping[TeamCallKey, str]
    consumed: set[TeamCallKey] = field(default_factory=set)

    def take(self, member_id: str | None, call_ids: set[str | None]) -> _Decided:
        """Return the decisions on ``member_id``'s calls among ``call_ids`` (None names the leader's own)."""
        keys = {(member_id, call_id) for call_id in call_ids if call_id is not None}
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
    paused: RunRecord
    plan: ResumePlan


def _plan_member_resumes(
    spec: TeamSpec,
    paused: TeamRunRecord,
    requirements: Sequence[RunRequirement],
    decisions: _TeamDecisions,
) -> list[_MemberResume]:
    """Plan one resume per paused member run, checking every member's decisions before any member runs."""
    members = {member.spec.id: member for member in spec.members}
    embedded = {member.run_id: member for member in paused.member_responses if isinstance(member, RunRecord)}
    by_run: dict[tuple[str, str], list[RunRequirement]] = {}
    for requirement in requirements:
        assert requirement.member_agent_id is not None
        if requirement.member_run_id is None:
            msg = f"Saved member approval of {requirement.member_agent_id!r} has no paused run identity; retry the request"
            raise ValueError(msg)
        by_run.setdefault((requirement.member_agent_id, requirement.member_run_id), []).append(requirement)
    plans: list[_MemberResume] = []
    for (member_id, run_id), group in by_run.items():
        member = members.get(member_id)
        if member is None:
            msg = (
                f"Paused member run {run_id!r} of team run {paused.run_id!r} belongs to "
                f"{member_id!r}, which is not a member of team {spec.id!r}"
            )
            raise ValueError(msg)
        member_record = embedded.get(run_id)
        if member_record is None:
            msg = f"Paused team run {paused.run_id!r} no longer holds member run {run_id!r}"
            raise ValueError(msg)
        decided = decisions.take(member_id, {requirement.tool_execution.tool_call_id for requirement in group})
        # The member resumes from its own record; its requirements name the member, as Agno's copies did, so the
        # resumed member row keeps the member fields the team run's requirements carry.
        named = replace(
            member_record,
            requirements=[
                replace(
                    requirement,
                    member_agent_id=member.spec.id,
                    member_agent_name=member.spec.name,
                    member_run_id=run_id,
                )
                for requirement in member_record.requirements or ()
            ],
        )
        plan = plan_resume(
            member.spec,
            named,
            approved=decided.approved,
            denied=decided.denied,
            external_results=decided.external_results,
        )
        plans.append(_MemberResume(member, named, plan))
    return plans


def _answer_delegations(
    record: TeamRunRecord,
    messages: list[Message],
    resumed: Sequence[TeamMember],
    results: Sequence[str],
) -> None:
    """Replace every delegation result that waited on a member with the members' results, as Agno's continuation did.

    A result waited on one of the ``resumed`` members when it is the notice the delegation gave for that member's
    pause (``waited_on_member``). The replacement lands on the run's tool entries and on the leader's request, so the
    leader reads the members' results where it read the pause notice. ``results`` holds only the members resumed in
    this call, so a collaborate sibling's text that completed earlier, or a member completed in an earlier chained
    resume, no longer reaches the leader; Agno 3.0.9 lost it the same way, and the request-body parity contract keeps
    that.
    """
    text = "\n".join([MEMBER_RESULTS_HEADING, *results])
    answered: set[str | None] = set()
    for tool in record.tools or ():
        if any(waited_on_member(tool, member) for member in resumed):
            tool.result = text
            answered.add(tool.tool_call_id)
    for index, message in enumerate(messages):
        if message.role == "tool" and message.tool_call_id in answered:
            messages[index] = replace(message, content=text)


def _base_leader_spec(spec: TeamSpec) -> AgentSpec:
    return AgentSpec(
        id=spec.id,
        name=spec.name,
        model=spec.model,
        system_message=spec.system_message,
        toolkits=spec.toolkits,
        tool_call_limit=spec.tool_call_limit,
    )


def _leader_spec(spec: TeamSpec, record: TeamRunRecord, run_member: MemberRunner, *, stream: bool) -> AgentSpec:
    """Return the leader as the loop runs it: the team's own tools, then the delegation tool bound to this run.

    ``stream`` is whether the run's leader requests stream, as Agno's team chose its delegation's result rules by it.
    """
    base = _base_leader_spec(spec)
    delegation = delegate_functions(spec.members, spec.mode, record=record, run_member=run_member, stream=stream)
    return replace(base, toolkits=(*base.toolkits, delegation))


def leader_tool_specs(spec: TeamSpec) -> tuple[ToolSpec, ...]:
    """Return the tool definitions every leader request offers: the team's own tools, then the delegation tool.

    The definitions do not depend on a run; a run binds the delegation tool to its own record and members.
    """

    async def no_member_runs(member: TeamMember, task: str) -> RunRecord:
        msg = f"the tool definitions of team {spec.id!r} run no member; {member.spec.id!r} was asked {task!r}"
        raise RuntimeError(msg)

    unbound = TeamRunRecord(run_id="", team_id=spec.id, team_name=spec.name)
    # The result rules a run's ``stream`` selects do not change the definitions.
    return prepare_run_tools(_leader_spec(spec, unbound, no_member_runs, stream=True)).specs()


@dataclass(frozen=True, slots=True)
class _MemberRuns:
    """Run and resume the members of one team run, forwarding their events tagged with the member.

    ``stream`` is whether the team run's leader requests stream; every member request follows it, as Agno ran and
    continued each member with its team run's ``stream``.
    """

    spec: TeamSpec
    record: TeamRunRecord
    ctx: RunContext
    emit: Emit
    persist: Persist[AnyRunRecord] | None
    stream: bool

    async def run(self, member: TeamMember, task: str) -> RunRecord:
        """Run ``member`` on the leader's task as a child of the team run; the task text is its whole input."""
        ctx = self._member_context(str(uuid4()))
        ended: list[RunRecord] = []
        stream = run_agent(
            member.spec,
            [Message(role="user", content=task)],
            ctx=ctx,
            hooks=member.hooks(ctx.run_id),
            persist=self._capture(ended),
            request_template=self._request_template(member),
        )
        return await self._finish(member, ctx.run_id, stream, ended)

    async def resume(self, member: TeamMember, paused: RunRecord, plan: ResumePlan) -> RunRecord:
        """Resume ``member``'s paused run, under that run's hooks, from its stored messages, which hold no history."""
        ended: list[RunRecord] = []
        stream = resume_planned(
            member.spec,
            plan,
            build_resume_request(paused=paused.messages, summary=None, history=[]),
            ctx=self._member_context(paused.run_id),
            hooks=member.hooks(paused.run_id),
            persist=self._capture(ended),
            request_template=self._request_template(member),
        )
        return await self._finish(member, paused.run_id, stream, ended)

    def _request_template(self, member: TeamMember) -> ModelRequest:
        return replace(member.request_template, stream=self.stream)

    def _member_context(self, run_id: str) -> RunContext:
        """Return a member run's context: the team run's, so its tools share the team's state, under its own run id."""
        return replace(self.ctx, run_id=run_id, parent_run_id=self.record.run_id)

    def _capture(self, ended: list[RunRecord]) -> Persist[RunRecord]:
        async def persist(record: RunRecord) -> None:
            ended.append(record)
            _keep_ended_member_run(self.record.ended_member_runs, record)
            if self.persist is not None:
                await self.persist(record)

        return persist

    async def _finish(
        self,
        member: TeamMember,
        run_id: str,
        stream: AsyncIterator[RunEvent],
        ended: list[RunRecord],
    ) -> RunRecord:
        """Forward the member's events and return its terminal record.

        A member whose run fails is stored ``ERROR`` by its loop and still reaches the leader, as Agno absorbed a
        member's error into the member's run, with the reason its record carries (``handover_content``); only
        cancellation propagates.
        """
        try:
            async with closing_async_stream(stream):
                async for event in stream:
                    if isinstance(event, RunFinished):
                        continue
                    tagged = (
                        replace(event, parent_run_id=self.record.run_id) if isinstance(event, ToolCompleted) else event
                    )
                    self.emit(MemberEvent(member.spec.id, member.spec.name, run_id, tagged))
        except Exception as error:
            logger.warning(
                "team_member_run_failed",
                team_id=self.spec.id,
                member_id=member.spec.id,
                run_id=run_id,
                error=str(error),
            )
        return ended[-1]


async def _forward(stream: AsyncIterator[RunEvent], emit: Emit) -> None:
    async with closing_async_stream(stream):
        async for event in stream:
            emit(event)


async def _team_stream(produce: Callable[[Emit], Awaitable[None]]) -> AsyncIterator[TeamRunEvent]:
    """Stream what ``produce`` emits while it runs the team, owning its cancellation."""
    stream = callback_event_stream(produce)
    async with closing_async_stream(stream):
        async for item in stream:
            # The producer's own result, None, comes last; everything before it is an event.
            if item is not None:
                yield cast("TeamRunEvent", item)


__all__ = [
    "MEMBER_RESULTS_HEADING",
    "MEMBER_RESULT_LINE",
    "MEMBER_TASK_COMPLETED",
    "Emit",
    "MemberEvent",
    "MemberLoopEvent",
    "TeamCallKey",
    "TeamRunEvent",
    "continue_team",
    "leader_tool_specs",
    "run_team",
    "team_paused_content",
    "team_usage",
    "team_usage_runs",
]
