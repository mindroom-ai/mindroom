"""Approved calls follow current waiting policy, gated calls can ask as their job, and nested execution keeps its owner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team
from agno.tools import Toolkit

from mindroom import approval_manager
from mindroom.agents import apply_tool_approval_capability
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.response_turn import paused_attempt_from_response
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.approvals import end_tool_call_approval
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.control import QueuedTurnSignal, queued_turn_signal_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    assembled_function,
    saved_jobs,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.tool_approval import BackgroundScriptToolOrigin
    from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome


class _NativeTools(Toolkit):
    def __init__(self, observed: list[int]) -> None:
        self.observed = observed
        super().__init__(name="native", tools=[self.native_step])
        bind_toolkit_construction(self, ToolConstruction("native_plugin", None))
        bind_toolkit_authority(self, authored_name="native_plugin")
        self.get_async_functions()["native_step"].requires_confirmation = True

    async def native_step(self, **options: int) -> str:
        """Observe native keywords without declaring a reserved managed parameter."""
        wait_timeout = options.get("wait_timeout", 7)
        self.observed.append(wait_timeout)
        return f"native:{wait_timeout}"


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("initially_excluded", [False, True])
async def test_approved_call_follows_exclusions_current_at_resume(
    tmp_path: Path,
    *,
    team: bool,
    initially_excluded: bool,
) -> None:
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
    storage = SqliteDb(db_file=str(tmp_path / "approvals.db"))

    def actor(model: DelegationModel) -> Agent | Team:
        install_tool_job_execution(model)
        kwargs = {"id": "leader", "model": model, "tools": [_NativeTools(observed)], "db": storage, "telemetry": False}
        return Team(**kwargs, members=[]) if team else Agent(**kwargs)

    try:
        async with execution_resources():
            with tool_runtime_context(context):
                paused = await actor(
                    DelegationModel(
                        id="test",
                        responses=[ModelResponse(tool_calls=[_call("native_step", "approved", wait_timeout=3)])],
                    ),
                ).arun("Execute", session_id=context.session_id)
                assert paused.status is RunStatus.paused
                captured = paused_attempt_from_response(
                    paused,
                    fallback_session_id=context.session_id,
                    fallback_run_id=paused.run_id,
                    toolkit_owners={("leader", "native_step"): "native_plugin"},
                )
                assert captured is not None

                # A changed exclusion list takes effect when the instance starts again.
                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["native_plugin"]
                pin_background_tool_jobs(config, paths)
                register_background_runtime(paths, runtime)
                for requirement in paused.requirements or ():
                    requirement.confirm()
                completed = await actor(
                    DelegationModel(id="test", responses=[ModelResponse(content="done")]),
                ).acontinue_run(
                    run_id=paused.run_id,
                    session_id=context.session_id,
                    requirements=paused.requirements,
                )
                for job in await runtime.list_jobs(owner=owner, depth=0):
                    await runtime.wait(job.job_id, owner=owner, depth=0)
        assert completed.status is RunStatus.completed
        # Newly managed, the reserved budget never reaches the tool; newly excluded, it is the tool's own argument.
        assert observed == ([7] if initially_excluded else [3])
        assert len(await runtime.list_jobs(owner=owner, depth=0)) == (1 if initially_excluded else 0)
    finally:
        await runtime.shutdown()
        storage.close()


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
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_child_tool", "child-call")]),
            ModelResponse(content="child done"),
        ],
    )
    install_tool_job_execution(model, depth=1)
    actor = Agent(id="leader", model=model, tools=[assembled_function(slow_child_tool)], telemetry=False)
    signal = QueuedTurnSignal()
    task = None
    try:
        async with execution_resources():
            with tool_runtime_context(context), queued_turn_signal_context(signal):
                task = asyncio.create_task(actor.arun("Work", session_id=context.session_id, metadata={}))
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                signal.notify()
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
        bind_toolkit_authority(self, authored_name="reports")

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
    settled: list[str] = field(default_factory=list)
    cards: object = field(default_factory=object)
    send_delivery: object = field(default_factory=object)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> BackgroundApprovalDecision:
        self.requested.append((origin.run_id, tool_name, arguments))
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
    stopped: bool = False
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

        async def stop_recorded(_job: BackgroundJob) -> bool:
            return self.stopped

        async def interrupt(job: BackgroundJob) -> BackgroundOutcome | None:
            # The coordinator's cleanup of a stopped tool call's job.
            return await end_tool_call_approval(self.runtime, job)

        self.runtime = await tool_job_runtime(self.tmp_path, cancel=interrupt, stopped=stop_recorded)
        pin_background_tool_jobs(self.config, self.paths)
        register_background_runtime(self.paths, self.runtime)
        self.cards = _Cards(asyncio.get_running_loop().create_future())
        self.monkeypatch.setattr(approval_manager, "get_approval_store", lambda: self.cards)
        self.storage = SqliteDb(db_file=str(self.tmp_path / "gated.db"))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.runtime.shutdown()
        self.storage.close()

    def agent(self, **budget: float) -> Agent:
        toolkit = apply_tool_approval_capability(
            _GatedTools(self.effects, self.gate),
            self.config,
            supports_native_tool_approval=True,
            approvals_as_jobs=True,
        )
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("write_report", "write-1", title="q3", **budget)]),
                ModelResponse(content="done"),
            ],
        )
        install_tool_job_execution(model)
        return Agent(id="leader", model=model, tools=[toolkit], db=self.storage, telemetry=False)

    def decide(self, status: Literal["approved", "denied"]) -> None:
        self.cards.decision.set_result(BackgroundApprovalDecision(status, None if status == "approved" else "No."))


def _tool_result(response: RunOutput) -> str:
    [tool] = response.tools or []
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
                reply = asyncio.create_task(run.agent().arun("Write", session_id=run.context.session_id))
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                # The card shows the exact arguments, and nothing ran before the decision.
                [(_, tool_name, arguments)] = run.cards.requested
                assert (tool_name, arguments, run.effects) == ("write_report", {"title": "q3"}, [])
                run.decide(decision)
                response = await asyncio.wait_for(reply, JOB_TEST_TIMEOUT)
        assert response.status is RunStatus.completed
        if decision == "approved":
            assert (_tool_result(response), run.effects) == ("wrote q3", ["q3"])
        else:
            assert "No." in _tool_result(response)
            assert run.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_reply_goes_on_while_its_call_waits_for_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stopped: bool,
) -> None:
    """After the wait, the reply continues and the job runs the call once approved, unless a Stop came first."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0.05) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await asyncio.wait_for(
                    run.agent().arun("Write", session_id=run.context.session_id),
                    JOB_TEST_TIMEOUT,
                )
                assert response.status is RunStatus.completed
                handle = json.loads(_tool_result(response))
                assert (handle["tool"], handle["status"], run.effects) == ("write_report", "awaiting_approval", [])
                run.stopped = stopped
                run.decide("approved")
                await wait_for_status(run.runtime, handle["job_id"], "cancelled" if stopped else "completed")
        assert run.effects == ([] if stopped else ["q3"])


@pytest.mark.asyncio
async def test_cancelled_job_denies_the_card_it_waits_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job cancelled while it waits for its approval leaves no card that could still approve the call."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0) as run:
        async with execution_resources():
            with tool_runtime_context(run.context):
                response = await run.agent().arun("Write", session_id=run.context.session_id)
                job_id = json.loads(_tool_result(response))["job_id"]
                await asyncio.wait_for(run.cards.posted.wait(), JOB_TEST_TIMEOUT)
                await run.runtime.cancel(job_id, owner=run.owner, depth=0)
                await wait_for_status(run.runtime, job_id, "cancelled")
        [(card_run_id, _, _)] = run.cards.requested
        assert card_run_id in run.cards.settled
        assert run.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["resources", "context"])
async def test_gated_call_that_cannot_become_a_job_never_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Without a job to ask through, a call that asks for its approval as its job fails instead of running."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=None) as run:
        agent = run.agent()
        if missing == "resources":
            with tool_runtime_context(run.context):
                response = await agent.arun("Write", session_id=run.context.session_id)
        else:
            async with execution_resources():
                response = await agent.arun("Write", session_id=run.context.session_id)
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
            response = await run.agent().arun("Write", session_id=run.context.session_id)
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
async def test_a_call_its_policy_approves_waits_like_any_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a human decision is bounded: a call the policy approves on its own blocks until it finishes."""
    async with _GatedRun(tmp_path, monkeypatch, approval_wait_timeout=0, gate=asyncio.Event()) as run:
        agent = run.agent()
        # The function asks as its job, but the policy approves this call, as a script or exemption can.
        run.config.tool_approval = ToolApprovalConfig(default="auto_approve")
        async with execution_resources():
            with tool_runtime_context(run.context):
                reply = asyncio.create_task(agent.arun("Write", session_id=run.context.session_id))
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
                    run.agent(wait_timeout=600).arun("Write", session_id=run.context.session_id),
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
                response = await run.agent().arun("Write", session_id=run.context.session_id)
                job_id = json.loads(_tool_result(response))["job_id"]
                waited = await asyncio.wait_for(JobTools(run.paths, run.owner).job("wait", job_id), JOB_TEST_TIMEOUT)
        assert json.loads(waited)["status"] == "awaiting_approval"
        assert run.effects == []
