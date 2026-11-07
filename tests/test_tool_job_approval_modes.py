"""Approved calls follow current waiting policy, gated calls can ask as their job, and nested execution keeps its owner."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import MagicMock

import pytest

from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.model_loop import EMPTY_REQUEST
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval import manager as approval_manager
from mindroom.approval.auto_approve_windows import start_auto_approve
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import (
    JOB_ENDED_REASON,
    REPLY_STOPPED_REASON,
    ApprovalCall,
    BackgroundApprovalDecision,
    EventJournalStore,
    ToolJobStore,
    tool_job_run_id,
)
from mindroom.llm.runs import RunStatus, ToolExecution
from mindroom.mid_turn import QueuedMessage
from mindroom.reply_scope import ReplyRuntime, SpanHandle, SpanSlot, _current_slot
from mindroom.tool_approval_grants import ApprovalOperation, grant_operation
from mindroom.tool_jobs import approvals as approvals_module
from mindroom.tool_jobs.approvals import ask_job_approvals, end_tool_call_approval
from mindroom.tool_jobs.client_cancel import cancel_stopped_reply_jobs
from mindroom.tool_jobs.control import JobControl, job_checkpoint, job_control_context, pending_human_messages_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE, AdaptedToolkit
from tests.conftest import FakeApprovalTransport
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.history_helpers import replay_settings
from tests.reply_span_helpers import reply_span
from tests.tool_approval_helpers import gated_functions
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    awaiting_approval,
    job_actor,
    job_card_decision,
    job_owner,
    job_tool,
    reserve_job_card,
    resume_job_turn,
    run_built_turn,
    run_job_turn,
    saved_job_card_decision,
    saved_jobs,
    start_job,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
    write_saved_job,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.event_journal import PrincipalStore
    from mindroom.llm.runs import RunRecord


class _NativeTools(Toolkit):
    def __init__(self, observed: list[int]) -> None:
        self.observed = observed
        super().__init__(name="native", tools=[self.native_step])
        bind_toolkit_construction(self, ToolConstruction("native_plugin", None))
        self.get_async_functions()["native_step"].requires_confirmation = True

    async def native_step(self, **options: int) -> str:
        """Observe native keywords without declaring a reserved managed parameter."""
        background_after = options.get("background_after", 7)
        self.observed.append(background_after)
        return f"native:{background_after}"


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_excluded", [False, True])
async def test_approved_call_follows_exclusions_current_at_resume(tmp_path: Path, *, initially_excluded: bool) -> None:
    """Waiting policy is decided when an approved call executes, so a restart with new exclusions applies to it."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(
            enabled=True,
            exclude_toolkits=["native_plugin"] if initially_excluded else [],
        ),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = context.execution_identity
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    observed: list[int] = []
    actor = job_actor(*toolkit_job_tools(_NativeTools(observed), authored_name="native_plugin").values())
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                paused = await run_job_turn(
                    actor,
                    ScriptedModel([calls(tool_call("native_step", "approved", background_after=3))]),
                    "Execute",
                    session_id=context.session_id,
                )
                assert paused.status is RunStatus.paused

                # A changed exclusion list takes effect when the instance starts again.
                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["native_plugin"]
                pin_background_tool_jobs(config, paths)
                register_background_runtime(paths, runtime)
                completed = await resume_job_turn(actor, ScriptedModel([answer("done")]), paused)
                for job in await runtime.list_jobs(owner=owner, depth=0):
                    await runtime.wait(job.job_id, owner=owner, depth=0)
        assert completed.status is RunStatus.completed
        # Newly managed, the reserved budget never reaches the tool; newly excluded, it is the tool's own argument.
        assert observed == ([7] if initially_excluded else [3])
        assert len(await runtime.list_jobs(owner=owner, depth=0)) == (1 if initially_excluded else 0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_nested_native_owner_keeps_slow_child_tool_after_human_signal(tmp_path: Path) -> None:
    """A delegated child without an outer managed job must never detach an orphan job."""
    started, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def slow_child_tool() -> str:
        started.set()
        await release.wait()
        effects.append("finished once")
        return effects[0]

    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["delegate"]),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    context = _delegate_runtime_context(config, paths)
    owner = context.execution_identity
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = ScriptedModel([calls(tool_call("slow_child_tool", "child-call")), answer("child done")])
    actor = job_actor(job_tool(slow_child_tool), depth=1)
    signal = asyncio.Event()
    task = None
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                task = asyncio.create_task(run_job_turn(actor, model, "Work", session_id=context.session_id))
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                signal.set()
                done, _ = await asyncio.wait({task}, timeout=0.05)
                assert not done, "Child returned before its tool finished"
                assert await runtime.list_jobs(owner=owner, depth=1) == []
                release.set()
                result = await asyncio.wait_for(task, JOB_TEST_TIMEOUT)
                assert result.tools is not None
                assert result.tools[0].result == "finished once"
                assert effects == ["finished once"]
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown()


class _GatedTools(Toolkit):
    """One policy-gated report writer whose every run is observable."""

    def __init__(self, effects: list[str], gate: asyncio.Event | None = None) -> None:
        self.effects = effects
        self.gate = gate
        super().__init__(name="reports", tools=[self.write_report])
        bind_toolkit_construction(self, ToolConstruction("reports", None))

    async def write_report(self, title: str) -> str:
        """Write one report."""
        self.effects.append(title)
        if self.gate is not None:
            await self.gate.wait()
        return f"wrote {title}"


@dataclass
class _Cards:
    """Stand in for the approval store: record each card a job posts and answer it when the test decides.

    Each card's call is reserved in the job journal, as the router reserves it, so the save of the job's end denies it.
    """

    decision: asyncio.Future[BackgroundApprovalDecision]
    journal: EventJournalStore
    posted: asyncio.Event = field(default_factory=asyncio.Event)
    requested: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)
    # The authored toolkit each card names as its call's origin.
    toolkits: list[str] = field(default_factory=list)
    # The entity and operation each card binds its timed approvals to.
    grants: list[tuple[str | None, ApprovalOperation | None]] = field(default_factory=list)
    # Each posted card's run and call.
    calls: list[tuple[str, str]] = field(default_factory=list)
    # The reply event each card names as the one whose call started its job.
    reply_events: list[str | None] = field(default_factory=list)
    transport: FakeApprovalTransport = field(default_factory=FakeApprovalTransport)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        owner_kind: str,
        tool_name: str,
        arguments: dict[str, object],
        entity_name: str | None = None,
        grant_operation: ApprovalOperation | None = None,
        response_event_id: str | None = None,
        **_kwargs: object,
    ) -> BackgroundApprovalDecision:
        assert owner_kind == "job"
        await reserve_job_card(self.journal, origin.run_id, origin.call_id, tool_name=tool_name)
        self.calls.append((origin.run_id, origin.call_id))
        self.reply_events.append(response_event_id)
        self.requested.append((origin.run_id, tool_name, arguments))
        self.toolkits.append(origin.toolkit_name)
        self.grants.append((entity_name, grant_operation))
        self.posted.set()
        return await asyncio.shield(self.decision)

    async def decisions(self) -> list[BackgroundApprovalDecision | None]:
        """Return the journal's decision of each posted card's call, None for a card that stays answerable."""
        return [await job_card_decision(self.journal, run_id, call_id) for run_id, call_id in self.calls]


@dataclass
class _GatedRun:
    """One leader agent whose gated call asks for its approval as its job."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    approval_wait_timeout: float | None
    effects: list[str] = field(default_factory=list)
    gate: asyncio.Event | None = None

    async def __aenter__(self) -> _GatedRun:
        self.config = Config(
            background_tool_jobs=BackgroundToolJobsConfig(
                enabled=True,
                approval_wait_timeout=self.approval_wait_timeout,
            ),
            tool_approval=ToolApprovalConfig(default="require_approval"),
            agents={"leader": AgentConfig(display_name="Leader")},
        )
        self.paths = _runtime_paths(self.tmp_path)
        self.context = _delegate_runtime_context(self.config, self.paths)
        self.owner = self.context.execution_identity

        async def interrupt(job: BackgroundJob) -> BackgroundOutcome | None:
            # The coordinator's cleanup of a stopped tool call's job.
            return end_tool_call_approval(job)

        self.runtime = await tool_job_runtime(self.tmp_path, cancel=interrupt)
        pin_background_tool_jobs(self.config, self.paths)
        register_background_runtime(self.paths, self.runtime)
        self.cards = _Cards(asyncio.get_running_loop().create_future(), self.runtime.journal)
        self.monkeypatch.setattr(approval_manager, "get_approval_store", lambda: self.cards)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.runtime.shutdown()

    async def turn(
        self,
        *,
        generated: bool = False,
        run_id: str = "run",
        titles: tuple[str, ...] = ("q3",),
        **budget: float,
    ) -> RunRecord:
        """Run the leader's reply that makes a gated call per title at once, its function a generated one when asked."""
        toolkit = _GatedTools(self.effects, self.gate)
        [function] = gated_functions(toolkit, self.config, authored_as="report_writer", approvals_as_jobs=True).values()
        # Authored under a name of its own, so a card names the authored toolkit, not the one built for it.
        [tool] = toolkit_job_tools(toolkit, authored_name="report_writer").values()
        made = [
            tool_call("write_report", f"write-{index}", title=title, **budget) for index, title in enumerate(titles, 1)
        ]
        model = ScriptedModel([calls(*made), answer("done")])
        if generated:
            return await run_job_turn(job_actor(), model, "Write", generated=(function,), run_id=run_id)
        return await run_job_turn(job_actor(replace(tool, function=function)), model, "Write", run_id=run_id)

    def decide(self, status: Literal["approved", "denied"]) -> None:
        self.cards.decision.set_result(BackgroundApprovalDecision(status, None if status == "approved" else "No."))


def _tool_result(record: RunRecord) -> str:
    [tool] = record.tools or []
    return str(tool.result)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "denied"])
async def test_gated_call_waits_for_its_approval_then_runs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: Literal["approved", "denied"],
) -> None:
    """Within the reply's wait, the decision on the job's card decides the call's own result."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(run.turn())
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                # The card shows the exact arguments, and nothing ran before the decision.
                [(_, tool_name, arguments)] = run.cards.requested
                assert (tool_name, arguments, run.effects) == ("write_report", {"title": "q3"}, [])
                assert run.cards.toolkits == ["report_writer"]
                # Its timed approvals cover this conversation's operation, as a paused reply's card would.
                assert run.cards.grants == [("leader", grant_operation(run.config, "write_report", {"title": "q3"}))]
                run.decide(decision)
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
        assert response.status is RunStatus.completed
        if decision == "approved":
            assert (_tool_result(response), run.effects) == ("wrote q3", ["q3"])
        else:
            assert "No." in _tool_result(response)
            assert run.effects == []


class _NewerMessageWaits:
    """The reply's queue, which holds a newer message of its requester until the reply ends."""

    def has_pending_human_messages(self) -> bool:
        return True

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]:
        return (QueuedMessage("$newer", None),)


@pytest.mark.asyncio
async def test_a_reply_told_to_pause_asks_for_no_more_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated call made after the pause notice posts no card; the reply ends instead.

    A newer message releases every wait at once, so each gated call became a job waiting on a card of its own. After a
    restart, the message a reply answered waits behind the follow-up of its interrupted job, and a model that ignored
    the notice posted cards until someone stopped the reply.
    """
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        toolkit = _GatedTools(run.effects)
        [function] = gated_functions(toolkit, run.config, authored_as="report_writer", approvals_as_jobs=True).values()
        [tool] = toolkit_job_tools(toolkit, authored_name="report_writer").values()
        # A model that never heeds the notice: every step calls the gated tool again.
        model = ScriptedModel(
            [*(calls(tool_call("write_report", f"write-{step}", title="q3")) for step in (1, 2, 3)), answer("done")],
        )
        build = AgentBuild(
            spec=AgentSpec(
                id="leader",
                name="Leader",
                model=model,
                system_messages=(),
                toolkits=(AdaptedToolkit(name="tools", functions=(function,), instructions=None),),
            ),
            prompt=PromptSpec(role="test", instructions=()),
            request_template=EMPTY_REQUEST,
            history_settings=replay_settings(),
            surface=AgentSurface(
                mode="standard",
                cli_shell=False,
                deferred=(),
                context_documents=(),
                context_files=(),
                knowledge_sources=(),
                output_file_policy=None,
            ),
            tool_policy=(),
            tool_jobs=job_actor(replace(tool, function=function)),
        )
        newer = asyncio.Event()
        newer.set()
        async with execution_resources():
            with (
                tool_runtime_context(run.context),
                pending_human_messages_context(newer),
                queued_message_signal_context(_NewerMessageWaits()),
            ):
                response = await asyncio.wait_for(
                    run_built_turn(build, config=run.config, runtime_paths=run.paths),
                    JOB_TEST_TIMEOUT,
                )
                jobs = await run.runtime.list_jobs(owner=run.owner, depth=0)
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
    assert response.status is RunStatus.completed
    # The first call's job asks once; the call the model made after the notice starts no job and ends the reply.
    assert len(jobs) == 1
    assert len(run.cards.requested) == 1
    assert len(model.requests) == 2
    assert response.tools is not None
    assert [(execution.tool_call_id, execution.is_error) for execution in response.tools] == [
        ("write-1", False),
        ("write-2", True),
    ]
    assert run.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_reply", "reply_event_id"),
    [(True, "$reply"), (True, None), (False, None)],
    ids=["shown-reply", "unshown-reply", "no-reply"],
)
async def test_a_job_card_names_the_reply_whose_call_started_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_reply: bool,
    reply_event_id: str | None,
) -> None:
    """A gated call's card names the event of the reply that made the call, when that reply already shows one."""
    handle = MagicMock(spec=SpanHandle)
    handle.reply = MagicMock(event_id=reply_event_id)
    handle.stopped.return_value = False
    token = _current_slot.set(SpanSlot(handle=handle) if in_reply else None)
    try:
        async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(run.turn())
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                run.decide("approved")
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
    finally:
        _current_slot.reset(token)
    assert response.status is RunStatus.completed
    assert run.cards.reply_events == [reply_event_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, ""])
async def test_a_call_denied_without_a_reason_tells_the_model_it_was_not_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str | None,
) -> None:
    """A denial that gives no reason answers the model with the note every other denial path records."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(run.turn())
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                run.cards.decision.set_result(BackgroundApprovalDecision("denied", reason))
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
        assert (_tool_result(response), run.effects) == ("Not approved by requester", [])


@pytest.mark.asyncio
async def test_reply_goes_on_while_its_call_waits_for_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the wait, the reply continues and the job runs the call once approved."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0.05) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await asyncio.wait_for(
                    run.turn(),
                    JOB_TEST_TIMEOUT,
                )
                assert response.status is RunStatus.completed
                handle = json.loads(_tool_result(response))
                assert (handle["tool"], handle["status"], run.effects) == ("write_report", "awaiting_approval", [])
                run.decide("approved")
                await wait_for_status(run.runtime, handle["job_id"], "completed")
        assert run.effects == ["q3"]


@pytest.mark.asyncio
async def test_cancelled_job_denies_the_card_it_waits_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job cancelled while it waits for its approval leaves no card that could still approve the call.

    The cancel request's own save denies the card, so it is not answerable while the job winds down.
    """
    saves: list[tuple[str, str | None]] = []
    original_save = ToolJobStore.save

    async def recording_save(
        store: ToolJobStore,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        approval_end_reason: str | None,
    ) -> None:
        saves.append((json.loads(job_json)["status"], approval_end_reason))
        await original_save(store, job_id, job_json, result_payload_json, approval_end_reason=approval_end_reason)

    monkeypatch.setattr(ToolJobStore, "save", recording_save)
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await run.turn()
                job_id = json.loads(_tool_result(response))["job_id"]
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                await run.runtime.cancel(job_id, owner=run.owner, depth=0)
                await wait_for_status(run.runtime, job_id, "cancelled")
            assert await run.cards.decisions() == [BackgroundApprovalDecision("denied", JOB_ENDED_REASON)]
        assert run.effects == []
    assert ("cancel_requested", JOB_ENDED_REASON) in saves


_REPLY_ROOM = "!replies:example.org"


def _reply_runtime(run: _GatedRun) -> tuple[PrincipalStore, ReplyRuntime]:
    """Return the leader's reply records and the bot instance that runs its replies, whose Stops reach its jobs."""
    principal = run.runtime.journal.principal("leader@replies")
    replies = ReplyRuntime(
        store=principal,
        entity_name="leader",
        generation="gen-test",
        retry_sources=lambda _room_id, _sources: None,
        approval_ended=lambda _reply_id: None,
        reply_stopped=partial(cancel_stopped_reply_jobs, run.paths),
    )
    return principal, replies


async def _turn_in_reply(
    run: _GatedRun,
    principal: PrincipalStore,
    replies: ReplyRuntime,
    question: str,
    titles: tuple[str, ...] = ("q3",),
) -> RunRecord:
    """Run the leader's gated calls in its reply to ``question``, shown as ``{question}:reply``, as a locked turn runs."""
    async with reply_span(
        principal,
        source_event_id=question,
        room_id=_REPLY_ROOM,
        placeholder_event_id=f"{question}:reply",
        runtime=replies,
    ) as handle:
        task = asyncio.current_task()
        assert task is not None
        # A Stop on the reply cancels this task, as it cancels a locked turn's.
        replies.spans.register(handle.span_id, task)
        return await run.turn(run_id=question, titles=titles)


async def _stop_reply(principal: PrincipalStore, replies: ReplyRuntime, question: str, *, by: str) -> None:
    """Stop the reply to ``question`` with the 🛑 reaction on its event, or as an edit of the question stops it."""
    if by == "reaction":
        stopped = await principal.replies.record_stop(
            f"{question}:reply",
            room_id=_REPLY_ROOM,
            receipt_order=1,
            may_wait=False,
            now_ns=time.time_ns(),
        )
    else:
        reply = await principal.replies.for_event(f"{question}:reply")
        assert reply is not None
        stopped = await principal.replies.stop(reply.reply_id, receipt_order=1, now_ns=time.time_ns())
    assert not isinstance(stopped, bool)
    assert stopped is not None
    await replies.run_effects(stopped.post_commit)


def _job_of(run: _GatedRun, question: str) -> BackgroundJob:
    """Return the job the reply to ``question`` started for its gated call."""
    [job] = [job for job in run.runtime.retained_jobs() if job.adapter["run_id"] == question]
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["reaction", "edit"])
async def test_stopping_a_reply_denies_the_approvals_its_jobs_wait_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    by: str,
) -> None:
    """A Stop ends the jobs its reply started that still wait for approval, so approving their cards runs nothing.

    The card's terminal decision says the reply was stopped, and another reply's job keeps waiting for its own card.
    """
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            other = asyncio.create_task(_turn_in_reply(run, principal, replies, "$other"))
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
            run.cards.posted.clear()
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)

            await _stop_reply(principal, replies, "$question", by=by)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            await asyncio.wait_for(wait_for_status(run.runtime, _job_of(run, "$question").job_id, "cancelled"), 5)
            assert await run.cards.decisions() == [None, BackgroundApprovalDecision("denied", REPLY_STOPPED_REASON)]
            assert _job_of(run, "$other").status == "awaiting_approval"
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
        assert run.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["require_approval", "auto_approve"])
async def test_a_stop_before_the_job_posts_its_card_leaves_no_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    """A Stop that lands after the reply started the job, while the policy still decides its call, still ends the job.

    The job posts no card, and a call the policy approves without one does not run either.
    """
    planning, plan = asyncio.Event(), asyncio.Event()
    original_plan = approvals_module.plan_approval_calls

    async def slow_plan(*args: object, **kwargs: object) -> object:
        planning.set()
        await plan.wait()
        return await original_plan(*args, **kwargs)

    monkeypatch.setattr(approvals_module, "plan_approval_calls", slow_plan)
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
            await asyncio.wait_for(planning.wait(), JOB_TEST_TIMEOUT)
            run.config.tool_approval = ToolApprovalConfig(default=policy)

            await _stop_reply(principal, replies, "$question", by="reaction")
            plan.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            await asyncio.wait_for(wait_for_status(run.runtime, _job_of(run, "$question").job_id, "cancelled"), 5)
        assert (run.cards.requested, run.effects) == ([], [])


@pytest.mark.asyncio
async def test_a_stopped_reply_s_jobs_get_no_approval_after_the_stop(tmp_path: Path) -> None:
    """A Stop cancels the reply's jobs that wait on their cards and refuses its other jobs every later approval.

    A job whose call runs passes every check that is not an approval and finishes; another reply's jobs are untouched.
    """
    runtime = await tool_job_runtime(tmp_path)
    ask, go = asyncio.Event(), asyncio.Event()

    async def runs() -> BackgroundOutcome:
        await go.wait()
        # The check a running job passes before each later tool call, as a working subagent's next call does.
        job_checkpoint()
        return BackgroundOutcome("completed", "done")

    async def asks_later() -> BackgroundOutcome:
        await ask.wait()
        job_checkpoint(approval=True)
        return await awaiting_approval(runtime, "asks later")

    jobs = {
        "waits": ("reply", partial(awaiting_approval, runtime, "waits")),
        "runs": ("reply", runs),
        "asks later": ("reply", asks_later),
        "other": ("other", partial(awaiting_approval, runtime, "other")),
    }
    try:
        for job_id, (reply_id, operation) in jobs.items():
            await start_job(
                runtime,
                job_id,
                tool_name="write_report",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
                reply_id=reply_id,
            )
        for job_id in ("waits", "other"):
            await asyncio.wait_for(wait_for_status(runtime, job_id, "awaiting_approval"), JOB_TEST_TIMEOUT)
        await runtime.cancel_stopped_reply("reply")
        await asyncio.wait_for(wait_for_status(runtime, "waits", "cancelled"), JOB_TEST_TIMEOUT)
        ask.set()
        go.set()
        await asyncio.wait_for(wait_for_status(runtime, "asks later", "cancelled"), JOB_TEST_TIMEOUT)
        await asyncio.wait_for(wait_for_status(runtime, "runs", "completed"), JOB_TEST_TIMEOUT)
        assert {job.job_id: job.status for job in runtime.retained_jobs()} == {
            "waits": "cancelled",
            "runs": "completed",
            "asks later": "cancelled",
            "other": "awaiting_approval",
        }
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_stop_during_shutdown_leaves_the_reply_s_jobs_to_the_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop that lands while the runtime shuts down only marks the reply's jobs; the shutdown alone settles them."""
    runtime = await tool_job_runtime(tmp_path)
    original_save = ToolJobStore.save
    saved: list[str] = []

    async def recording(
        store: ToolJobStore,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        approval_end_reason: str | None,
    ) -> None:
        saved.append(json.loads(job_json)["status"])
        await original_save(store, job_id, job_json, result_payload_json, approval_end_reason=approval_end_reason)

    await start_job(
        runtime,
        "waits",
        tool_name="write_report",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=partial(awaiting_approval, runtime, "waits"),
        reply_id="reply",
    )
    await asyncio.wait_for(wait_for_status(runtime, "waits", "awaiting_approval"), JOB_TEST_TIMEOUT)
    monkeypatch.setattr(ToolJobStore, "save", recording)
    shutdown = asyncio.create_task(runtime.shutdown())
    await asyncio.sleep(0)
    assert runtime._shutdown_task is not None
    await runtime.cancel_stopped_reply("reply")
    await asyncio.wait_for(shutdown, JOB_TEST_TIMEOUT)
    assert saved == ["interrupted"]


@pytest.mark.asyncio
async def test_a_call_on_its_way_to_its_job_when_its_reply_stops_starts_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call the reply recorded before its Stop, whose job the Stop's look for the reply's jobs overtook, starts none.

    The reply's second call queues for its job's start while the Stop saves the first job's cancel request, so its
    start runs only after the Stop looked; its job is cancelled before it launches, so it posts no card to approve.
    """
    saving_cancel, second_waits = asyncio.Event(), asyncio.Event()
    original_save, original_start = ToolJobStore.save, ToolJobRuntime.start

    async def held_cancel(
        store: ToolJobStore,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        approval_end_reason: str | None,
    ) -> None:
        if json.loads(job_json)["status"] == "cancel_requested":
            saving_cancel.set()
            await second_waits.wait()
        await original_save(store, job_id, job_json, result_payload_json, approval_end_reason=approval_end_reason)

    async def second_starts_during_the_stop(runtime: ToolJobRuntime, job_id: str, **options: Any) -> object:  # noqa: ANN401
        if options["adapter"]["tool_call_id"] == "write-2":
            await saving_cancel.wait()
            second_waits.set()
        return await original_start(runtime, job_id, **options)

    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        monkeypatch.setattr(ToolJobStore, "save", held_cancel)
        monkeypatch.setattr(ToolJobRuntime, "start", second_starts_during_the_stop)
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question", ("first", "second")))
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)

            await asyncio.wait_for(_stop_reply(principal, replies, "$question", by="reaction"), JOB_TEST_TIMEOUT)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            assert second_waits.is_set()
            jobs = run.runtime.retained_jobs()
            for job in jobs:
                await asyncio.wait_for(wait_for_status(run.runtime, job.job_id, "cancelled"), JOB_TEST_TIMEOUT)
            assert [job.adapter["tool_call_id"] for job in jobs] == ["write-1", "write-2"]
            assert await run.cards.decisions() == [BackgroundApprovalDecision("denied", REPLY_STOPPED_REASON)]
        assert run.effects == []


@pytest.mark.asyncio
async def test_a_decision_that_reaches_the_runtime_before_its_replys_stop_runs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval that arrived just before the Stop, and waits for the runtime lock ahead of it, still runs nothing.

    Another job write holds the lock while the decision and then the Stop queue for it, so the job would pass its
    approval before the Stop looks for the reply's jobs.
    """
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
            job = _job_of(run, "$question")
            execution = run.runtime._entries[job.job_id].task
            assert execution is not None
            lock = run.runtime._lock
            await lock.acquire()
            run.decide("approved")
            while len(lock._waiters or ()) < 1:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            stop = asyncio.create_task(_stop_reply(principal, replies, "$question", by="reaction"))
            while len(lock._waiters or ()) < 2:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            lock.release()

            await asyncio.wait_for(stop, JOB_TEST_TIMEOUT)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), JOB_TEST_TIMEOUT)
            assert run.effects == []
            await asyncio.wait_for(wait_for_status(run.runtime, job.job_id, "cancelled"), JOB_TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_a_stop_whose_cancel_save_fails_still_runs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose cancel request the Stop could not save never runs its call, and approving it saves the cancel.

    The failed save surfaces from the Stop's effects as any failed effect does; the job stays marked, so approving its
    card refuses the job, which denies the card, saying the reply was stopped, and settles the job cancelled.
    """
    original_save = ToolJobStore.save
    failures: list[str] = []

    async def failing_once(
        store: ToolJobStore,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        approval_end_reason: str | None,
    ) -> None:
        if approval_end_reason == REPLY_STOPPED_REASON and not failures:
            failures.append(job_id)
            msg = "the journal is unavailable"
            raise OSError(msg)
        await original_save(store, job_id, job_json, result_payload_json, approval_end_reason=approval_end_reason)

    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
            monkeypatch.setattr(ToolJobStore, "save", failing_once)

            with pytest.raises(OSError, match="the journal is unavailable"):
                await _stop_reply(principal, replies, "$question", by="reaction")

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            job = _job_of(run, "$question")
            assert (failures, job.status, await run.cards.decisions()) == ([job.job_id], "awaiting_approval", [None])
            execution = run.runtime._entries[job.job_id].task
            assert execution is not None
            run.decide("approved")
            await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), JOB_TEST_TIMEOUT)

            await asyncio.wait_for(wait_for_status(run.runtime, job.job_id, "cancelled"), JOB_TEST_TIMEOUT)
            assert await run.cards.decisions() == [BackgroundApprovalDecision("denied", REPLY_STOPPED_REASON)]
        assert run.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["taking-decision", "taking-denial", "starting-wait"])
async def test_stop_during_approval_state_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """A Stop during either approval-state save prevents card posting and gated tool entry."""
    saving, release = asyncio.Event(), asyncio.Event()
    original_save = ToolJobStore.save
    target_status = "awaiting_approval" if phase == "starting-wait" else "running"

    async def held_save(
        store: ToolJobStore,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        approval_end_reason: str | None,
    ) -> None:
        status = json.loads(job_json)["status"]
        if status == target_status and not saving.is_set():
            saving.set()
            await release.wait()
        await original_save(store, job_id, job_json, result_payload_json, approval_end_reason=approval_end_reason)

    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run, execution_resources():
        monkeypatch.setattr(ToolJobStore, "save", held_save)
        principal, replies = _reply_runtime(run)
        parent = stop = None
        with tool_runtime_context(run.context):
            try:
                parent = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
                if phase != "starting-wait":
                    await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                    run.decide("denied" if phase == "taking-denial" else "approved")
                await asyncio.wait_for(saving.wait(), JOB_TEST_TIMEOUT)
                job = _job_of(run, "$question")
                entry = run.runtime._entries[job.job_id]
                execution = entry.task
                assert execution is not None
                stop = asyncio.create_task(_stop_reply(principal, replies, "$question", by="reaction"))
                async with asyncio.timeout(JOB_TEST_TIMEOUT):
                    while not entry.control.reply_stopped:  # noqa: ASYNC110
                        await asyncio.sleep(0.001)
                release.set()
                await asyncio.wait_for(stop, JOB_TEST_TIMEOUT)
                await asyncio.wait_for(asyncio.gather(parent, execution, return_exceptions=True), JOB_TEST_TIMEOUT)
                assert run.effects == [], "call executed after Stop marked it during the decision's save"
                if phase == "starting-wait":
                    assert run.cards.requested == [], "new approval card posted after Stop marked the job"
                await asyncio.wait_for(wait_for_status(run.runtime, job.job_id, "cancelled"), JOB_TEST_TIMEOUT)
            finally:
                release.set()
                for task in (parent, stop):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (parent, stop) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_stop_leaves_a_job_running_the_call_its_policy_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated job whose call the requester's auto-approve window passed without a card runs on after its reply's Stop.

    The policy granted the approval before the Stop, and the job asks for no other, so nothing refuses it.
    """
    async with (
        _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None, gate=asyncio.Event()) as run,
        execution_resources(),
    ):
        threaded = replace(run.owner, thread_id="$thread", resolved_thread_id="$thread")
        run.context = _delegate_runtime_context(run.config, run.paths, execution_identity=threaded)
        assert threaded.requester_id is not None
        start_auto_approve(
            run.paths,
            room_id=run.context.room_id,
            thread_id="$thread",
            requester_id=threaded.requester_id,
            duration=timedelta(minutes=5),
        )
        principal, replies = _reply_runtime(run)
        with tool_runtime_context(run.context):
            stopped = asyncio.create_task(_turn_in_reply(run, principal, replies, "$question"))
            while not run.effects:  # noqa: ASYNC110
                await asyncio.sleep(0.01)

            await _stop_reply(principal, replies, "$question", by="reaction")

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopped, JOB_TEST_TIMEOUT)
            job = _job_of(run, "$question")
            assert job.status == "running"
            assert run.gate is not None
            run.gate.set()
            await asyncio.wait_for(wait_for_status(run.runtime, job.job_id, "completed"), JOB_TEST_TIMEOUT)
        assert (run.cards.requested, run.effects) == ([], ["q3"])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["resources", "context", "managed mode", "toolkit binding"])
async def test_gated_call_that_cannot_become_a_job_never_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Without a job to ask through, a call that asks for its approval as its job fails instead of running.

    That covers a call inside another job, which runs inline, and a function without its toolkit's binding, as the
    SDK generates them.
    """
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        generated = missing == "toolkit binding"
        if missing == "resources":
            with tool_runtime_context(run.context):
                response = await run.turn()
        elif missing == "context":
            async with execution_resources():
                response = await run.turn()
        else:
            inside_a_job = job_control_context(JobControl()) if missing == "managed mode" else nullcontext()
            async with execution_resources():
                with tool_runtime_context(run.context), inside_a_job:
                    response = await run.turn(generated=generated)
        assert "needs approval" in _tool_result(response)
        assert (run.effects, run.cards.requested) == ([], [])


@pytest.mark.asyncio
async def test_shutdown_while_a_call_waits_for_approval_says_it_did_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike other interrupted work, a call stopped while it asked for approval has no effects to check."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0) as run, execution_resources():
        with tool_runtime_context(run.context):
            response = await run.turn()
            job_id = json.loads(_tool_result(response))["job_id"]
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
    # Leaving the run shuts its runtime down while the call still waits for its approval.
    job = (await saved_jobs(tmp_path))[job_id]
    assert job.status == "interrupted"
    assert job.result == "The runtime stopped while this call waited for its approval, so it did not run."
    [(card_run_id, card_call_id)] = run.cards.calls
    assert await saved_job_card_decision(tmp_path, card_run_id, card_call_id) == BackgroundApprovalDecision(
        "denied",
        JOB_ENDED_REASON,
    )
    assert run.effects == []


@pytest.mark.asyncio
async def test_a_call_stopped_after_its_approval_may_have_taken_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved call's job no longer waits for approval, so a stop never tells the model a call that ran did not."""
    async with (
        _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0, gate=asyncio.Event()) as run,
        execution_resources(),
    ):
        with tool_runtime_context(run.context):
            response = await run.turn()
            job_id = json.loads(_tool_result(response))["job_id"]
            await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
            run.decide("approved")
            while not run.effects:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            assert run.runtime._entries[job_id].job.status == "running"
    # Leaving the run shuts its runtime down while the approved call still runs.
    job = (await saved_jobs(tmp_path))[job_id]
    assert job.status == "interrupted"
    assert "did not run" not in (job.result or "")
    assert run.effects == ["q3"]


@pytest.mark.asyncio
async def test_a_restart_denies_the_card_a_crashed_call_waited_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card a stopped process posted would stay answerable, and answering it would do nothing."""
    job = BackgroundJob(
        job_id="j" * 64,
        owner=job_owner(),
        tool_name="write_report",
        depth=0,
        toolkit_name="reports",
        source_event_id="$turn",
        source_kind="message",
        status="awaiting_approval",
    )
    await write_saved_job(tmp_path, job.job_id, json.dumps(asdict(job)))
    # No approval runtime exists yet to deny the card.
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: None)

    async def interrupt(saved: BackgroundJob) -> BackgroundOutcome | None:
        return end_tool_call_approval(saved)

    runtime = await tool_job_runtime(tmp_path, cancel=interrupt)
    run_id = tool_job_run_id(job.job_id)
    try:
        await reserve_job_card(runtime.journal, run_id, "write-1", tool_name="write_report")
        await runtime.recover()
        assert await job_card_decision(runtime.journal, run_id, "write-1") == BackgroundApprovalDecision(
            "denied",
            JOB_ENDED_REASON,
        )
    finally:
        await runtime.shutdown()
    recovered = (await saved_jobs(tmp_path))[job.job_id]
    assert (recovered.status, recovered.result) == (
        "interrupted",
        "The runtime stopped while this call waited for its approval, so it did not run.",
    )


@pytest.mark.asyncio
async def test_a_call_its_policy_approves_waits_like_any_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a human decision is bounded: a call the policy approves on its own blocks until it finishes."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0, gate=asyncio.Event()) as run:
        reply_toolkit = _GatedTools(run.effects, run.gate)
        [function] = gated_functions(reply_toolkit, run.config, authored_as="reports", approvals_as_jobs=True).values()
        [tool] = toolkit_job_tools(reply_toolkit, authored_name="reports").values()
        # The function asks as its job, but the policy approves this call, as a script or exemption can.
        run.config.tool_approval = ToolApprovalConfig(default="auto_approve")
        model = ScriptedModel([calls(tool_call("write_report", "write-1", title="q3")), answer("done")])
        async with execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(run_job_turn(job_actor(replace(tool, function=function)), model, "Write"))
                while not run.effects:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                assert not reply.done()
                assert run.gate is not None
                run.gate.set()
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
        assert (_tool_result(response), run.cards.requested) == ("wrote q3", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("window_thread", ["reply", "other"])
async def test_a_job_call_skips_its_card_only_inside_the_requesters_auto_approve_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window_thread: str,
) -> None:
    """A gated call that asks as its job runs unasked inside the window of the conversation its reply answers."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        threaded = replace(run.owner, thread_id="$thread", resolved_thread_id="$thread")
        run.context = _delegate_runtime_context(run.config, run.paths, execution_identity=threaded)
        assert threaded.requester_id is not None
        start_auto_approve(
            run.paths,
            room_id=run.context.room_id,
            thread_id="$thread" if window_thread == "reply" else "$other",
            requester_id=threaded.requester_id,
            duration=timedelta(minutes=5),
        )
        async with execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(run.turn())
                if window_thread == "other":
                    await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                    run.decide("denied")
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
        if window_thread == "reply":
            assert (_tool_result(response), run.cards.requested, run.effects) == ("wrote q3", [], ["q3"])
        else:
            assert len(run.cards.requested) == 1
            assert run.effects == []


@pytest.mark.asyncio
async def test_a_calls_own_budget_does_not_outlast_its_approval_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long budget the model asks for still goes on once the decision has taken the approval wait."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0.05) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await asyncio.wait_for(
                    run.turn(background_after=600),
                    JOB_TEST_TIMEOUT,
                )
        handle = json.loads(_tool_result(response))
        assert (handle["status"], run.effects) == ("awaiting_approval", [])


@pytest.mark.asyncio
async def test_retrieving_a_job_that_awaits_approval_waits_no_longer_than_its_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting on the job afterwards is bounded the same way, so the reply never sits until the decision."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0.05) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await run.turn()
                job_id = json.loads(_tool_result(response))["job_id"]
                waited = await asyncio.wait_for(JobTools(run.paths).job("wait", job_id), JOB_TEST_TIMEOUT)
        assert json.loads(waited)["status"] == "awaiting_approval"
        assert run.effects == []


@pytest.mark.asyncio
async def test_a_job_that_asks_late_still_gets_the_whole_approval_wait(tmp_path: Path) -> None:
    """The approval allowance counts from when the job asks, however long it worked before, and anew for each ask."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    loop = asyncio.get_running_loop()
    asked: list[float] = []
    decided, asked_again = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        # The job works longer than the allowance before it asks, then asks a second time once the first is decided.
        await asyncio.sleep(0.3)
        for _ in range(2):
            await runtime.set_awaiting_approval("job-1", awaiting=True)
            asked.append(loop.time())
            if len(asked) == 2:
                asked_again.set()
            await decided.wait()
            decided.clear()
            await runtime.set_awaiting_approval("job-1", awaiting=False)
        return BackgroundOutcome("completed", "ok")

    try:
        _, claim = await runtime.start("job-1", tool_name="t", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("job-1", owner=owner, depth=0, claim=claim, approval_timeout=0.2)
        assert waited.job.status == "awaiting_approval"
        assert loop.time() - asked[0] >= 0.2
        decided.set()
        await asyncio.wait_for(asked_again.wait(), JOB_TEST_TIMEOUT)
        waited = await runtime.wait("job-1", owner=owner, depth=0, approval_timeout=0.2)
        assert waited.job.status == "awaiting_approval"
        assert loop.time() - asked[1] >= 0.2
    finally:
        decided.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_work_allowance_leaves_out_the_time_the_job_waits_for_approval(tmp_path: Path) -> None:
    """A wait bounded by the job's work keeps waiting through its approval, then ends after that much work."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    loop = asyncio.get_running_loop()
    approved: list[float] = []
    decided, finish = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await runtime.set_awaiting_approval("job-1", awaiting=True)
        await decided.wait()
        await runtime.set_awaiting_approval("job-1", awaiting=False)
        approved.append(loop.time())
        await finish.wait()
        return BackgroundOutcome("completed", "ok")

    try:
        _, claim = await runtime.start("job-1", tool_name="t", depth=0, adapter={}, owner=owner, operation=operation)
        waiting = asyncio.create_task(runtime.wait("job-1", owner=owner, depth=0, claim=claim, work_timeout=0.1))
        # The approval takes longer than the allowance, which waits on through it.
        await asyncio.sleep(0.3)
        assert not waiting.done()
        decided.set()
        waited = await asyncio.wait_for(waiting, JOB_TEST_TIMEOUT)
        assert waited.job.status == "running"
        assert waited.claim is None
        assert loop.time() - approved[0] >= 0.05
    finally:
        decided.set()
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_job_cards_offer_timed_approval_only_for_calls_the_policy_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As on a paused reply, a background child's call that asked for confirmation itself is approved only once."""
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    runtime = await tool_job_runtime(tmp_path)
    cards = _Cards(asyncio.get_running_loop().create_future(), runtime.journal)
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    tools = [
        ToolExecution(
            tool_call_id=call_id,
            tool_name="write_report",
            tool_args={"title": call_id},
            requires_confirmation=True,
            approval_type=approval_type,
        )
        for call_id, approval_type in (("policy", POLICY_CONFIRMATION_APPROVAL_TYPE), ("authored", None))
    ]
    calls = [
        ApprovalCall(
            tool_call_id=str(tool.tool_call_id),
            tool_name="write_report",
            invoking_agent="child",
            toolkit_name="reports",
            expires_at_ns=time.time_ns() + 60_000_000_000,
        )
        for tool in tools
    ]
    owner = job_owner()
    decided: list[tuple[bool, str | None]] = []

    async def operation() -> BackgroundOutcome:
        decided.extend(
            await ask_job_approvals(
                runtime,
                "job-1",
                tools,
                calls,
                owner=owner,
                config=config,
                runtime_paths=_runtime_paths(tmp_path),
                response_event_id=None,
            ),
        )
        return BackgroundOutcome("completed", "ok")

    try:
        _, claim = await runtime.start("job-1", tool_name="t", depth=0, adapter={}, owner=owner, operation=operation)
        await runtime.wait("job-1", owner=owner, depth=0, claim=claim)
    finally:
        await runtime.shutdown()
    assert decided == [(True, None), (True, None)]
    assert cards.grants == [("parent", grant_operation(config, "write_report", {"title": "policy"})), ("parent", None)]
