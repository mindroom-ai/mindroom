"""Shared tool job runtime and assembled-function construction for tests."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import uuid4

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.approval.resume import ResumeRunner
from mindroom.bot import AgentBot
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.execution import _drive_delegations
from mindroom.delegation.state import DelegationChild
from mindroom.event_journal import ApprovalCall, EventJournalStore, ToolJobStore, approval_arguments_digest
from mindroom.event_journal.offloading import ThreadOffload
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.history.request_messages import build_resume_request
from mindroom.history.types import HistoryScope
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.matrix.identity import MatrixID
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.participation_hook import participation_hook
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import persist_to
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.authorization import toolkit_authority
from mindroom.tool_jobs.execution import JobActor, job_definitions_hook, with_job_admission
from mindroom.tool_jobs.execution_authority import JobTool
from mindroom.tool_jobs.notices import job_notice_hook
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    BackgroundJob,
    BackgroundOutcome,
    ToolJobRuntime,
    _parse_saved_job,
)
from mindroom.tool_system.agno_toolkits import Toolkit, adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.tool_function import AdaptedToolkit
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context
from tests.fakes.scripted_model import ScriptedModel
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    import pytest

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.run import Persist
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import ChildResponseRunner
    from mindroom.llm.provider import ModelProvider
    from mindroom.participation import ParticipationGate
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.invocation import ToolMiddleware, ToolOutcome
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from tests.fakes.scripted_model import ScriptStep


# One generous bound for waits that must eventually finish; a slow CI host must not turn them into failures.
JOB_TEST_TIMEOUT = 30
_LEADER_SCOPE = HistoryScope(kind="agent", scope_id="leader")


def _authorize_all(_job: BackgroundJob) -> bool:
    return True


def _allow_execution(_owner: ToolExecutionIdentity, _tool: JobTool, _arguments: Mapping[str, Any]) -> None:
    return None


async def _no_cleanup(_job: BackgroundJob) -> None:
    return None


def tool_job_journal(storage_root: Path) -> EventJournalStore:
    """Open the event journal of one storage root, as one process does; another open is another process."""
    return EventJournalStore.open_sqlite(event_journal_sqlite_path(storage_root))


class ProcessRuntime(ToolJobRuntime):
    """One process's runtime over its own journal connection, which shutting down closes as the process exit would."""

    def __init__(
        self,
        journal: EventJournalStore,
        *,
        authorize: Callable[[BackgroundJob], bool],
        authorize_execution: Callable[[ToolExecutionIdentity, JobTool, Mapping[str, Any]], None],
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]],
        denied: Callable[[BackgroundJob], bool] | None = None,
    ) -> None:
        super().__init__(
            journal.tool_jobs(uuid4().hex),
            authorize=authorize,
            authorize_execution=authorize_execution,
            cancel=cancel,
            # Without its own denial rule, a test treats every unauthorized job as proven denied.
            denied=denied or (lambda job: not authorize(job)),
        )
        self._journal = journal

    async def shutdown(self) -> None:
        """Shut down in order, then close this process's journal connection."""
        try:
            await super().shutdown()
        finally:
            await self.close_journal()

    async def close_journal(self) -> None:
        """Close this process's journal connection, as its exit does with or without an orderly shutdown."""
        await self._journal.close()


async def tool_job_runtime(
    storage_root: Path,
    *,
    authorize: Callable[[BackgroundJob], bool] = _authorize_all,
    authorize_execution: Callable[[ToolExecutionIdentity, JobTool, Mapping[str, Any]], None] = _allow_execution,
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] = _no_cleanup,
    denied: Callable[[BackgroundJob], bool] | None = None,
) -> ProcessRuntime:
    """Start one process's runtime whose grants allow every job and call and whose adapters need no extra cleanup.

    It owns the saved jobs, fencing any earlier runtime, but leaves restoring them to `recover`.
    """
    runtime = ProcessRuntime(
        tool_job_journal(storage_root),
        authorize=authorize,
        authorize_execution=authorize_execution,
        cancel=cancel,
        denied=denied,
    )
    await runtime._store.take_ownership()
    return runtime


def _inline_statement[Result](_offload: ThreadOffload, call: Callable[[], Result]) -> asyncio.Future[Result]:
    work = asyncio.get_running_loop().create_future()
    try:
        work.set_result(call())
    except Exception as error:
        work.set_exception(error)
    return work


def run_journal_statements_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run journal statements on the event loop, so an idle loop means every save has landed or failed."""
    monkeypatch.setattr(ThreadOffload, "submit", _inline_statement)


# The journal's own saves, which every interception wraps however often a test intercepts them.
_JOURNAL_ACCEPT, _JOURNAL_SAVE = ToolJobStore.accept, ToolJobStore.save
# Runs around one job save with its decoded snapshot and payload; raising fails the save, before or after it lands.
type SaveHook = Callable[[dict[str, Any], object | None], Awaitable[None] | None]


def intercept_job_saves(
    monkeypatch: pytest.MonkeyPatch,
    *,
    before: SaveHook | None = None,
    after: SaveHook | None = None,
) -> None:
    """Run hooks around every journal save of a job, as a failing or slow database would; no hooks restores saving."""
    original_accept, original_save = _JOURNAL_ACCEPT, _JOURNAL_SAVE

    async def hook(callback: SaveHook | None, job_json: str, payload_json: str | None) -> None:
        if callback is not None:
            result = callback(json.loads(job_json), None if payload_json is None else json.loads(payload_json))
            if result is not None:
                await result

    async def accept(store: ToolJobStore, job_id: str, job_json: str) -> None:
        await hook(before, job_json, None)
        await original_accept(store, job_id, job_json)
        await hook(after, job_json, None)

    async def save(store: ToolJobStore, job_id: str, job_json: str, result_payload_json: str | None = None) -> None:
        await hook(before, job_json, result_payload_json)
        await original_save(store, job_id, job_json, result_payload_json)
        await hook(after, job_json, result_payload_json)

    monkeypatch.setattr(ToolJobStore, "accept", accept)
    monkeypatch.setattr(ToolJobStore, "save", save)


async def saved_jobs(storage_root: Path) -> dict[str, BackgroundJob]:
    """Read every job the journal of one storage root holds, as the next process would recover it."""
    journal = tool_job_journal(storage_root)
    try:
        return {saved.job_id: _parse_saved_job(saved) for saved in await journal.tool_jobs("reader").load_all()}
    finally:
        await journal.close()


async def write_saved_job(storage_root: Path, job_id: str, job_json: str) -> None:
    """Save one snapshot as is, as an earlier process might have left it; the writer takes the jobs over."""
    journal = tool_job_journal(storage_root)
    try:
        store = journal.tool_jobs(uuid4().hex)
        await store.take_ownership()
        if job_id in {saved.job_id for saved in await journal.tool_jobs("reader").load_all()}:
            await store.save(job_id, job_json)
        else:
            await store.accept(job_id, job_json)
    finally:
        await journal.close()


async def saved_payload(storage_root: Path, job_id: str) -> object:
    """Read the outcome payload saved for one job, or None."""
    journal = tool_job_journal(storage_root)
    try:
        payload = await journal.tool_jobs("reader").load_payload(job_id)
    finally:
        await journal.close()
    return None if payload is None else json.loads(payload)


async def lookup(runtime: ToolJobRuntime, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
    """Inspect an exact job as a caller would reach it, failing like any access when that caller cannot see it."""
    async with runtime._lock:
        return deepcopy(runtime._entry(job_id, owner, depth).job)


def pending_outcomes(runtime: ToolJobRuntime) -> list[BackgroundJob]:
    """Ended outcomes a reply could still collect: unconsumed, unclaimed, and authorized."""
    return [
        entry.job
        for entry in runtime._entries.values()
        if entry.job.status in TERMINAL_STATUSES
        and not entry.job.consumed
        and entry.claim is None
        and runtime._authorize(entry.job)
    ]


def pending_outcome(runtime: ToolJobRuntime, job_id: str) -> BackgroundJob | None:
    """The pending outcome of one job, if a reply could still collect it."""
    return next((job for job in pending_outcomes(runtime) if job.job_id == job_id), None)


def toolkit_job_tools(
    toolkit: Toolkit,
    *,
    authored_name: str = "application",
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, JobTool]:
    """Offer every function of ``toolkit`` as agent assembly does, authored as ``authored_name``.

    ``snapshot`` is the actor's execution scope and file access, as ``authority_snapshot`` freezes it; only such
    functions may become jobs.
    """
    authority = toolkit_authority(snapshot or {}, toolkit)
    return {
        function.name: JobTool(function=function, toolkit_name=authored_name, toolkit=toolkit, authority=authority)
        for function in adapt_toolkit(toolkit).functions
    }


def job_tool(
    entrypoint: Callable[..., object],
    *,
    authored_name: str = "application",
    snapshot: Mapping[str, Any] | None = None,
) -> JobTool:
    """Offer one callable as a function of a MindRoom-assembled toolkit; see ``toolkit_job_tools``."""
    [tool] = toolkit_job_tools(
        Toolkit(name=authored_name, tools=[entrypoint]),
        authored_name=authored_name,
        snapshot=snapshot,
    ).values()
    return tool


def job_actor(
    *tools: JobTool,
    name: str = "leader",
    team: bool = False,
    depth: int = 0,
) -> JobActor:
    """An agent, or a team, offering ``tools`` as tool jobs see it."""
    return JobActor(name=name, team=team, depth=depth, tools={tool.function.name: tool for tool in tools})


async def call_job_tool(
    actor: JobActor,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    call_id: str | None = "call",
    run_id: str = "run",
    session_id: str = "session",
    session_state: dict[str, Any] | None = None,
    middleware: tuple[ToolMiddleware, ...] = (),
) -> ToolOutcome:
    """Run one model call of the actor's function ``name`` through job admission, as the owned loop dispatches it."""
    context = ToolCallContext(
        run_id=run_id,
        session_id=session_id,
        call_id=call_id,
        session_state={} if session_state is None else session_state,
    )
    return await execute_tool_call(
        actor.tools[name].function,
        arguments,
        context,
        middleware=with_job_admission(actor, middleware),
    )


def _job_turn(
    actor: JobActor,
    model: ModelProvider,
    *,
    session_id: str,
    session_state: dict[str, Any] | None,
    store: ConversationStore | None,
    generated: tuple[ToolFunction, ...],
    middleware: tuple[ToolMiddleware, ...],
    participation: ParticipationGate | None = None,
) -> tuple[AgentSpec, LoopHooks, ConversationSession, Persist[RunRecord] | None]:
    functions = (*(tool.function for tool in actor.tools.values()), *generated)
    spec = AgentSpec(
        id=actor.name,
        name=actor.name,
        model=model,
        system_message=None,
        toolkits=(AdaptedToolkit(name="tools", functions=functions, instructions=None),),
    )
    hooks = LoopHooks(
        # The job definitions project before the participation gate decides, as an agent's turn installs them.
        before_request=(
            job_definitions_hook(actor),
            *((participation_hook(participation, model),) if participation is not None else ()),
        ),
        tool_middleware=with_job_admission(actor, middleware),
        after_tool_batch=(job_notice_hook(actor.name),),
    )
    session = ConversationSession(
        session_id=session_id,
        kind="team" if actor.team else "agent",
        owner_id=actor.name,
        session_state={} if session_state is None else session_state,
    )
    return spec, hooks, session, None if store is None else persist_to(store, session)


async def run_job_turn(
    actor: JobActor,
    model: ModelProvider,
    prompt: str = "start",
    *,
    run_id: str = "run",
    session_id: str = "session",
    session_state: dict[str, Any] | None = None,
    store: ConversationStore | None = None,
    generated: tuple[ToolFunction, ...] = (),
    middleware: tuple[ToolMiddleware, ...] = (),
    participation: ParticipationGate | None = None,
) -> RunRecord:
    """Run one turn of the actor on the owned loop with the job hooks an agent's turn installs.

    ``session_state`` is the session's state the run's tools share; ``store`` saves the run and that state as a
    reply does. ``generated`` are functions the framework offers beside the actor's assembled ones, which never
    become jobs. ``participation`` gates the reply as an agent's turn does, deciding after the job definitions
    project.
    """
    spec, hooks, session, persist = _job_turn(
        actor,
        model,
        session_id=session_id,
        session_state=session_state,
        store=store,
        generated=generated,
        middleware=middleware,
        participation=participation,
    )
    run = RunContext(run_id=run_id, session_id=session_id, session_state=session.session_state)
    events = run_agent(spec, [Message(role="user", content=prompt)], ctx=run, hooks=hooks, persist=persist)
    finished = [event.record async for event in events if isinstance(event, RunFinished)]
    return finished[-1]


async def resume_job_turn(
    actor: JobActor,
    model: ModelProvider,
    paused: RunRecord,
    *,
    session_state: dict[str, Any] | None = None,
    store: ConversationStore | None = None,
    middleware: tuple[ToolMiddleware, ...] = (),
) -> RunRecord:
    """Resume ``paused`` with every call it waits on approved, as an approval card's decision resumes a reply."""
    session_id = paused.session_id or "session"
    spec, hooks, session, persist = _job_turn(
        actor,
        model,
        session_id=session_id,
        session_state=session_state,
        store=store,
        generated=(),
        middleware=middleware,
    )
    approved = [
        ApprovalCall(
            tool_call_id=str(requirement.tool_execution.tool_call_id),
            tool_name=str(requirement.tool_execution.tool_name),
            invoking_agent=actor.name,
            toolkit_name=actor.tools[str(requirement.tool_execution.tool_name)].toolkit_name,
            expires_at_ns=2**62,
            arguments_digest=approval_arguments_digest(requirement.tool_execution.tool_args),
        )
        for requirement in paused.requirements or ()
    ]
    events = resume_agent(
        spec,
        paused,
        build_resume_request(paused=paused.messages, summary=None, history=[]),
        approved=approved,
        ctx=RunContext(run_id=paused.run_id, session_id=session_id, session_state=session.session_state),
        hooks=hooks,
        persist=persist,
    )
    finished = [event.record async for event in events if isinstance(event, RunFinished)]
    return finished[-1]


async def run_built_turn(
    build: AgentBuild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    prompt: str = "start",
    run_id: str = "run",
    session_id: str = "session",
) -> RunRecord:
    """Run one turn of an agent ``create_agent`` built, on the owned loop with the hooks every agent turn installs."""
    name = build.spec.id
    hooks = agent_loop_hooks(
        build,
        HookTurn(
            config=config,
            runtime_paths=runtime_paths,
            model_name="default",
            store=None,
            session=ConversationSession(session_id=session_id, kind="agent", owner_id=name),
            scope=None,
            replay_plan=None,
            prompt_id=None,
        ),
        agent_name=name,
    )
    finished = [
        event.record
        async for event in run_agent(
            replace(build.spec, system_message=prompt_for_turn(build)),
            [Message(role="user", content=prompt)],
            ctx=RunContext(run_id=run_id, session_id=session_id),
            hooks=hooks,
            request_template=build.request_template,
        )
        if isinstance(event, RunFinished)
    ]
    return finished[-1]


async def start_job(runtime: ToolJobRuntime, job_id: str, **options: Any) -> BackgroundJob:  # noqa: ANN401
    """Accept a job, then drop the claim its start minted, as a foreground caller whose wait ended early does."""
    job, claim = await runtime.start(job_id, **options)
    await runtime.release_wait(job_id, claim)
    return job


async def keep_child(_child: DelegationChild) -> None:
    """A cancellation cleanup that leaves the child as its operation settled it."""


async def start_delegation_job(runtime: ToolJobRuntime, child: DelegationChild, **options: Any) -> BackgroundJob:  # noqa: ANN401
    """Accept a native child, then drop the claim its start minted, as `start_job` does."""
    options.setdefault("cancel", keep_child)
    job, claim = await start_delegation(runtime, child, **options)
    await runtime.release_wait(job.job_id, claim)
    return job


async def wait_for_status(runtime: ToolJobRuntime, job_id: str, status: str) -> None:
    """Wait until the runtime publishes the expected status for an accepted job."""
    entry = runtime._entries[job_id]
    while entry.job.status != status:
        await entry.changed.wait()


def backdate_job(runtime: ToolJobRuntime, job_id: str, updated_at: datetime) -> None:
    """Backdate a saved job in memory so retention treats it as old."""
    entry = runtime._entries[job_id]
    entry.job = replace(entry.job, updated_at=updated_at.isoformat())


def job_owner() -> ToolExecutionIdentity:
    """The parent identity most runtime tests start jobs under."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="parent",
        requester_id="@alice:test",
        room_id="!room:test",
        thread_id=None,
        resolved_thread_id="$root",
        session_id="parent-session",
    )


def job_child(job_id: str = "a" * 32) -> DelegationChild:
    """A native child owned by `job_owner`, with a reusable subagent ID."""
    return DelegationChild(
        delegation_id=job_id,
        parent_tool_call_id="call",
        caller_agent_name="parent",
        child_agent_name="child",
        task="research",
        session_id="child-session",
        run_id="run",
        model_name="default",
        depth=1,
        execution_identity={},
        subagent_id="b" * 32,
    )


def managed_team_config(tmp_path: Path) -> Config:
    """An enabled instance whose `team` routes through `lead`, which may delegate to `worker`."""
    access = ResponderAccessConfig(users=["@human:localhost"], current_room_members=False)
    return bind_runtime_paths(
        Config(
            background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
            agents={
                "lead": AgentConfig(display_name="Lead", delegate_to=["worker"], access=access),
                "worker": AgentConfig(display_name="Worker", access=access),
            },
            teams={"team": TeamConfig(display_name="Team", role="Work", agents=["lead"], access=access)},
        ),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def completed_delegation_job() -> BackgroundJob:
    """A finished native delegation from `lead` to `worker`, as `managed_team_config` routes it."""
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="lead",
        requester_id="@human:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:localhost_$thread",
        transport_agent_name="team",
    )
    child = DelegationChild(
        delegation_id="job_123",
        parent_tool_call_id="call_123",
        caller_agent_name="lead",
        child_agent_name="worker",
        task="Inspect files",
        session_id="child_session",
        run_id="child_run",
        model_name="default",
        depth=1,
        execution_identity={},
    )
    return BackgroundJob(
        job_id="job_123",
        tool_name="worker",
        depth=0,
        kind="delegation",
        adapter={"child": asdict(child)},
        owner=owner,
        status="completed",
        result="Finished @worker",
    )


def team_coordinator(tmp_path: Path, config: Config) -> ToolJobRuntimeCoordinator:
    """A coordinator whose only bot is the `team` transport."""
    bot = MagicMock(spec=AgentBot)
    bot.matrix_id = MatrixID.parse("@mindroom_team:localhost")
    paths = test_runtime_paths(tmp_path)
    return ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda name: bot if name == "team" else None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, paths.storage_root),
    )


async def finish_delegation_job(coordinator: ToolJobRuntimeCoordinator) -> BackgroundJob:
    """Run `completed_delegation_job` to a saved outcome whose claim is released."""
    fixture = completed_delegation_job()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved answer")

    job = await start_delegation_job(
        coordinator.runtime,
        delegation_child(fixture),
        owner=fixture.owner,
        operation=operation,
    )
    result = await coordinator.runtime.wait(job.job_id, owner=job.owner, depth=0)
    await coordinator.runtime.release_wait(job.job_id, result.claim)
    return result.job


async def awaiting_approval(runtime: ToolJobRuntime, job_id: str) -> BackgroundOutcome:
    """Wait for approval cards forever, as a paused background child does until a decision or cancellation."""
    await runtime.set_awaiting_approval(job_id, awaiting=True)
    await asyncio.Event().wait()
    raise AssertionError


class SpanRecorder:
    """Stand in for the reply span's tool-call records, refusing every call once a Stop committed."""

    def __init__(self, *, stopped: bool) -> None:
        self.stopped = stopped
        self.records: list[tuple[str, str, dict[str, object], object]] = []

    async def started(self, tool_name: str, args: Mapping[str, object]) -> str | None:
        """Record a call before it runs, or refuse it after a Stop."""
        if self.stopped:
            raise asyncio.CancelledError
        self.records.append(("started", tool_name, dict(args), None))
        return "record-1"

    async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
        """Record what a call returned."""
        assert call_id == "record-1"
        self.records.append(("finished", tool_name, dict(args), result))


def job_coordinator(tmp_path: Path, runtime: ToolJobRuntime, bot: MagicMock) -> ToolJobRuntimeCoordinator:
    """A coordinator around a test runtime whose grants allow every job, delivering through one bot."""
    paths = test_runtime_paths(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: None,
        bot_provider=lambda _name: bot,
        agent_reply_memberships=MagicMock(),
        journal_provider=lambda: tool_job_journal(paths.storage_root),
    )
    coordinator._runtime = runtime
    # The journal the runtime keeps its jobs in, which the replies share.
    coordinator._journal = tool_job_journal(tmp_path)
    return coordinator


def job_context(tmp_path: Path, owner: ToolExecutionIdentity) -> ToolRuntimeContext:
    """The tool runtime context of a reply in the conversation of ``owner``, on a managed team config."""
    paths = test_runtime_paths(tmp_path)
    return replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )


async def cancel_settled(
    runtime: ToolJobRuntime,
    job_id: str,
    *,
    owner: ToolExecutionIdentity,
    depth: int,
) -> BackgroundJob:
    """Cancel a job as its caller, then wait until the runtime drained it to its outcome."""
    requested = await runtime.cancel(job_id, owner=owner, depth=depth)
    drain = runtime._entries[job_id].drain
    return requested if drain is None else await asyncio.shield(drain)


@dataclass
class DelegatingLeader:
    """The leader as a reply builds it, whose turns the native driver advances with a test's child runner.

    Each turn builds the leader with ``create_agent`` and runs it on the owned loop with the hooks every agent turn
    installs; ``drive`` hands its delegations to the native driver, which continues it through the production resume.
    """

    config: Config
    paths: RuntimePaths
    identity: ToolExecutionIdentity
    agents: ScriptedAgents
    depth: int = 0
    store: ConversationStore = field(init=False)
    session: ConversationSession = field(init=False)
    build: AgentBuild | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Give the agents their Matrix identities and open the leader's conversation store."""
        entity_ids(self.config, self.paths)
        self.store = create_session_storage("leader", self.config, self.paths, self.identity)
        self.session = ConversationSession(session_id=self.identity.session_id or "", kind="agent", owner_id="leader")

    @property
    def model(self) -> ScriptedModel:
        """The leader's provider for its current turn."""
        return self.agents.models["leader"]

    async def _persist(self, record: RunRecord) -> None:
        await persist_to(self.store, self.session)(record)

    async def turn(self, *turns: Sequence[ScriptStep], run_id: str = "parent-run") -> RunRecord:
        """Run the leader on ``turns`` until it completes or pauses on its delegations."""
        self.agents.models["leader"] = ScriptedModel(list(turns))
        self.build = build = create_agent(
            "leader",
            self.config,
            self.paths,
            self.identity,
            history_storage=self.store,
            delegation_depth=self.depth,
            supports_native_tool_approval=True,
            include_interactive_questions=False,
        )
        hooks = agent_loop_hooks(
            build,
            HookTurn(
                config=self.config,
                runtime_paths=self.paths,
                model_name="default",
                store=self.store,
                session=self.session,
                scope=_LEADER_SCOPE,
                replay_plan=None,
                prompt_id=None,
            ),
            agent_name="leader",
        )
        events = run_agent(
            replace(build.spec, system_message=prompt_for_turn(build)),
            [Message(role="user", content="Delegate")],
            ctx=RunContext(
                run_id=run_id,
                session_id=self.session.session_id,
                user_id=self.identity.requester_id,
                session_state=self.session.session_state,
            ),
            hooks=hooks,
            persist=self._persist,
            request_template=build.request_template,
        )
        finished = [event.record async for event in events if isinstance(event, RunFinished)]
        record = finished[-1]
        assert isinstance(record, RunRecord)
        return record

    async def drive(self, record: RunRecord, run_child: ChildResponseRunner) -> RunRecord:
        """Advance ``record``'s delegations with ``run_child`` and continue the leader with their results."""
        assert self.build is not None
        runner = ResumeRunner(
            build=self.build,
            config=self.config,
            runtime_paths=self.paths,
            agent_name="leader",
            model_name="default",
            store=self.store,
            session=self.session,
            scope=_LEADER_SCOPE,
            user_id=self.identity.requester_id,
        )
        return await _drive_delegations(
            record,
            store=self.store,
            session=self.session,
            continue_parent=runner.continue_run,
            agent_name="leader",
            run_child=run_child,
            config=self.config,
            runtime_paths=self.paths,
            execution_identity=self.identity,
            delegation_depth=self.depth,
        )

    async def reply(
        self,
        *turns: Sequence[ScriptStep],
        run_child: ChildResponseRunner,
        run_id: str = "parent-run",
    ) -> RunRecord:
        """Run one leader reply whose delegations ``run_child`` runs."""
        return await self.drive(await self.turn(*turns, run_id=run_id), run_child)
