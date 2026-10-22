"""Approved calls follow current waiting policy, gated calls can ask as their job, and nested execution keeps its owner."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import ApprovalCall, BackgroundApprovalDecision
from mindroom.llm.runs import RunStatus, ToolExecution
from mindroom.tool_approval_grants import ApprovalOperation, grant_operation
from mindroom.tool_jobs.approvals import ask_job_approvals, end_tool_call_approval
from mindroom.tool_jobs.control import JobControl, job_control_context, pending_human_messages_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_approval_helpers import gated_functions
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    job_actor,
    job_owner,
    job_tool,
    resume_job_turn,
    run_job_turn,
    saved_jobs,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
    write_saved_job,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
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
    owner = build_execution_identity_from_runtime_context(context)
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
    owner = build_execution_identity_from_runtime_context(context)
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
    """Stand in for the approval store: record each card a job posts and answer it when the test decides."""

    decision: asyncio.Future[BackgroundApprovalDecision]
    posted: asyncio.Event = field(default_factory=asyncio.Event)
    requested: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)
    # The entity and operation each card binds its timed approvals to.
    grants: list[tuple[str | None, ApprovalOperation | None]] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    cards: object = field(default_factory=object)
    send_delivery: object = field(default_factory=object)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
        entity_name: str | None = None,
        grant_operation: ApprovalOperation | None = None,
        **_kwargs: object,
    ) -> BackgroundApprovalDecision:
        self.requested.append((origin.run_id, tool_name, arguments))
        self.grants.append((entity_name, grant_operation))
        self.posted.set()
        return await asyncio.shield(self.decision)

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int:
        assert reason
        self.settled.append(run_id)
        return 0


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
        self.owner = build_execution_identity_from_runtime_context(self.context)

        async def interrupt(job: BackgroundJob) -> BackgroundOutcome | None:
            # The coordinator's cleanup of a stopped tool call's job.
            return await end_tool_call_approval(self.runtime, job)

        self.runtime = await tool_job_runtime(self.tmp_path, cancel=interrupt)
        pin_background_tool_jobs(self.config, self.paths)
        register_background_runtime(self.paths, self.runtime)
        self.cards = _Cards(asyncio.get_running_loop().create_future())
        self.monkeypatch.setattr(approval_manager, "get_approval_store", lambda: self.cards)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.runtime.shutdown()

    async def turn(self, *, generated: bool = False, **budget: float) -> RunRecord:
        """Run the leader's reply that makes the gated call, its function a generated one when ``generated``."""
        toolkit = _GatedTools(self.effects, self.gate)
        [function] = gated_functions(toolkit, self.config, approvals_as_jobs=True).values()
        [tool] = toolkit_job_tools(toolkit, authored_name="reports").values()
        model = ScriptedModel([calls(tool_call("write_report", "write-1", title="q3", **budget)), answer("done")])
        if generated:
            return await run_job_turn(job_actor(), model, "Write", generated=(function,))
        return await run_job_turn(job_actor(replace(tool, function=function)), model, "Write")

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
    """A job cancelled while it waits for its approval leaves no card that could still approve the call."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await run.turn()
                job_id = json.loads(_tool_result(response))["job_id"]
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                await run.runtime.cancel(job_id, owner=run.owner, depth=0)
                await wait_for_status(run.runtime, job_id, "cancelled")
        [(card_run_id, _, _)] = run.cards.requested
        assert card_run_id in run.cards.settled
        assert run.effects == []


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
    [(card_run_id, _, _)] = run.cards.requested
    assert card_run_id in run.cards.settled
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
    cards = _Cards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)

    async def interrupt(saved: BackgroundJob) -> BackgroundOutcome | None:
        return await end_tool_call_approval(runtime, saved)

    runtime = await tool_job_runtime(tmp_path, cancel=interrupt)
    try:
        await runtime.recover()
    finally:
        await runtime.shutdown()
    recovered = (await saved_jobs(tmp_path))[job.job_id]
    assert (recovered.status, recovered.result) == (
        "interrupted",
        "The runtime stopped while this call waited for its approval, so it did not run.",
    )
    assert cards.settled == [f"tool-job:{job.job_id}"]


@pytest.mark.asyncio
async def test_a_call_its_policy_approves_waits_like_any_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a human decision is bounded: a call the policy approves on its own blocks until it finishes."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0, gate=asyncio.Event()) as run:
        reply_toolkit = _GatedTools(run.effects, run.gate)
        [function] = gated_functions(reply_toolkit, run.config, approvals_as_jobs=True).values()
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
                waited = await asyncio.wait_for(JobTools(run.paths, run.owner).job("wait", job_id), JOB_TEST_TIMEOUT)
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
async def test_job_cards_offer_timed_approval_only_for_calls_the_policy_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As on a paused reply, a background child's call that asked for confirmation itself is approved only once."""
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    cards = _Cards(asyncio.get_running_loop().create_future())
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
    runtime = await tool_job_runtime(tmp_path)
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
