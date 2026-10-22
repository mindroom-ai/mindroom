"""Native background jobs retain exact execution and approval ownership.

The leader runs as ``DelegatingLeader`` builds it: a reply's agent on the owned loop whose turns pause on their
delegations, which the native driver runs, here with each test's own child runner, and continues through the
production resume. A child that needs approvals runs as a real child turn of ``code``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import ai
from mindroom.approval import manager as approval_manager
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.custom_tools.job import JobTools
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation import recovery as delegation_recovery
from mindroom.delegation.background import delegation_child
from mindroom.delegation.lifecycle import prepare_child_turn, start_child_turn
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.sessions import load_retained_subagent_turn, subagent_recovery_lock
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.tool_jobs.approvals import _approval_run_id
from mindroom.tool_jobs.control import (
    JobControl,
    job_control_context,
    job_owns_execution,
    pending_human_messages_context,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    BackgroundOutcome,
    register_background_runtime,
)
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.scripted_agent_helpers import registered_toolkit
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    SpanRecorder,
    cancel_settled,
    intercept_job_saves,
    job_child,
    lookup,
    saved_jobs,
    start_delegation_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
    from pathlib import Path

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_jobs.runtime import ToolJobRuntime

_REQUESTER = "@alice:example.org"


def _identity(session_id: str = "parent") -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", "leader", _REQUESTER, "!room:example.org", None, None, session_id)


def _config(
    *,
    child_tools: Sequence[str] = (),
    rules: Sequence[ApprovalRuleConfig] = (),
    jobs: bool = True,
) -> Config:
    """Return the leader that delegates to ``code``, which the requester may reach; ``jobs`` turns tool jobs on."""
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=list(child_tools)),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval=ToolApprovalConfig(rules=list(rules)),
        ),
        "code",
        users=[_REQUESTER],
    )
    config.background_tool_jobs.enabled = jobs
    return config


class _ReportTools(Toolkit):
    """The child's report tools; each run is a side effect the test watches."""

    effects: ClassVar[list[str]] = []

    def __init__(self) -> None:
        super().__init__(name="reports", tools=[self.write_report, self.delete_report])

    async def write_report(self) -> str:
        """Write the report."""
        type(self).effects.append("written")
        return "Report written"

    async def delete_report(self) -> str:
        """Delete the report."""
        type(self).effects.append("deleted")
        return "Report deleted"


@contextmanager
def _registered_reports() -> Iterator[type[_ReportTools]]:
    """Register the ``reports`` toolkit for one test and restore the registries afterwards."""
    _ReportTools.effects.clear()
    with registered_toolkit("reports", _ReportTools):
        yield _ReportTools


def _tool_message(record: RunRecord, call_id: str) -> str:
    """Return what one call of the leader's run answered, as the run stores it."""
    return next(str(message.content) for message in record.messages or [] if message.tool_call_id == call_id)


async def _unused_child(*_args: object, **_kwargs: object) -> str:
    msg = "This delegation must never start a child"
    raise AssertionError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize(
    ("tool_name", "budget", "depth", "excluded"),
    [
        ("run_subagent", "bad", 0, False),
        ("run_subagent", 0, 1, False),
        ("continue_subagent", -1, 0, False),
        ("run_subagent", 0, 0, True),
        ("run_subagent", None, 0, True),
        ("continue_subagent", 0, 0, True),
    ],
)
async def test_invalid_native_wait_resolves_exact_requirement_without_child_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restored: bool,
    tool_name: str,
    budget: object,
    depth: int,
    excluded: bool,
) -> None:
    """Fresh and restored external requirements return correctable tool failures before admission."""
    paths = _runtime_paths(tmp_path)
    config = _config()
    owner = _identity()
    if excluded:
        config.background_tool_jobs.exclude_toolkits.append("delegate")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, owner, agents, depth=depth)
    arguments = {"task": "must-not-run", "agent_name": "code", "background_after": budget}
    if tool_name == "continue_subagent":
        arguments = {"subagent_id": "missing", "message": "must-not-run", "background_after": budget}

    try:
        async with execution_resources():
            with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
                paused = await leader.turn(calls(tool_call(tool_name, "invalid", **arguments)), answer("corrected"))
                assert paused.status is RunStatus.paused
                if restored:
                    paused = RunRecord.from_dict(paused.to_dict())
                response = await asyncio.wait_for(leader.drive(paused, _unused_child), JOB_TEST_TIMEOUT)
        assert response.status is RunStatus.completed
        assert response.content == "corrected"
        rejected = next(tool for tool in response.tools or [] if tool.tool_call_id == "invalid")
        assert rejected.tool_call_error
        assert "background_after" in str(rejected.result)
        assert rejected.tool_args == arguments
        assert any(
            message.tool_call_id == "invalid" and "background_after" in str(message.content)
            for message in leader.model.requests[-1].messages
        )
        assert DelegationState.from_metadata(response.metadata).children == []
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["restart", "stop", "deletion"])
async def test_parent_cancellation_during_job_admission_keeps_accepted_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """Cancellation before start returns must still relinquish the parent's child ownership.

    A Stop or deletion that commits during admission leaves the child running as its job, which only a cancel ends.
    """
    paths = _runtime_paths(tmp_path)
    config = _config()
    owner = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, owner, agents)
    written, executing, release_writer = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children: list[DelegationChild] = []

    async def blocked_writer(_job: dict[str, object], _payload: object) -> None:
        written.set()
        await release_writer.wait()

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        children.append(child)
        executing.set()
        await asyncio.Event().wait()
        raise AssertionError

    recorder = SpanRecorder(stopped=False)
    stop = ending != "restart"
    try:
        with (
            tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)),
            recording_tool_calls(recorder),
        ):
            response = await leader.turn(calls(tool_call("run_subagent", "call", task="Research", agent_name="code")))
            intercept_job_saves(monkeypatch, before=blocked_writer)
            driving = asyncio.create_task(leader.drive(response, run_child))
            await asyncio.wait_for(written.wait(), JOB_TEST_TIMEOUT)
            # A Stop or deletion commits while the job is admitted, before any job existed for it to record.
            recorder.stopped = stop
            if ending == "stop":
                request_task_cancel(driving, cancel_source="user_stop")
            else:
                driving.cancel()
            release_writer.set()
            with pytest.raises(asyncio.CancelledError):
                await driving
            state = DelegationState.from_metadata(response.metadata)
            assert state.children == []
            assert len(state.hooks) == 1
            assert next(iter(state.hooks.values())).after_called
            assert len(await runtime.list_jobs(owner=owner, depth=0)) == 1
            await asyncio.wait_for(executing.wait(), JOB_TEST_TIMEOUT)
            assert children[0].status == "running"
    finally:
        release_writer.set()
        await runtime.shutdown()


@dataclass
class _JobApprovalCards:
    """Stand in for the approval store: record each card a job posts and answer it when the test decides."""

    decision: asyncio.Future[BackgroundApprovalDecision]
    posted: asyncio.Event = field(default_factory=asyncio.Event)
    requested: list[tuple[str, str, str]] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    cards: object = field(default_factory=object)
    send_delivery: object = field(default_factory=object)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        tool_name: str,
        **_kwargs: object,
    ) -> BackgroundApprovalDecision:
        self.requested.append((origin.run_id, origin.call_id, tool_name))
        self.posted.set()
        return await asyncio.shield(self.decision)

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int:
        assert reason
        self.settled.append(run_id)
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("detach", "human"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("approval", [None, "approved", "denied", "cancelled"])
@pytest.mark.parametrize("exclude_after_acceptance", [False, True])
async def test_native_background_result_runs_child_once(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detach: bool,
    human: bool,
    approval: Literal["approved", "denied", "cancelled"] | None,
    exclude_after_acceptance: bool,
) -> None:
    """A released parent cannot cancel the child, the job owns its approval cards, and later waits read its result."""
    paths = _runtime_paths(tmp_path)
    config = _config(
        child_tools=["reports"],
        rules=[ApprovalRuleConfig(match="write_report", action="require_approval")],
    )
    identity = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = _JobApprovalCards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    code = ScriptedModel(
        [*([calls(tool_call("write_report", "write-once"))] if approval else []), answer("Exact child result")],
    )
    agents = ScriptedAgents({"code": code})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)
    release = asyncio.Event()
    completed = asyncio.Event()
    children: list[DelegationChild] = []
    signal = asyncio.Event()
    real_child = ai.run_delegated_child_response

    async def run_child(child: DelegationChild, **kwargs: Any) -> str:  # noqa: ANN401
        children.append(child)
        if human:
            signal.set()
        if detach:
            await release.wait()
        result = await real_child(child, **kwargs)
        completed.set()
        return result

    async def decide() -> None:
        """Answer the job's card as the requester would, while a parent may still be waiting."""
        await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
        job_id = children[0].delegation_id
        await wait_for_status(runtime, job_id, "awaiting_approval")
        assert cards.requested == [(_approval_run_id(job_id), "write-once", "write_report")]
        assert reports.effects == []
        if exclude_after_acceptance:
            config.background_tool_jobs.exclude_toolkits.append("delegate")
        if approval == "cancelled":
            assert '"status": "cancel_requested"' in str(await JobTools(paths, identity).job("cancel", job_id))
            await wait_for_status(runtime, job_id, "cancelled")
            cancelled = await read_child_run(children[0], config, paths)
            assert cancelled is not None
            assert cancelled.status is RunStatus.cancelled
            assert cards.settled == [_approval_run_id(job_id)]
            return
        status = "denied" if approval == "denied" else "approved"
        cards.decision.set_result(BackgroundApprovalDecision(status, "Not now" if status == "denied" else None))

    recorder = SpanRecorder(stopped=False)
    with _registered_reports() as reports:
        try:
            with (
                tool_runtime_context(
                    replace(
                        _delegate_runtime_context(config, paths, execution_identity=identity),
                        membership_turn_id="$native-reader",
                    ),
                ),
                pending_human_messages_context(signal),
                recording_tool_calls(recorder),
            ):
                budget = 0.01 if detach and not human else None
                driving = asyncio.create_task(
                    leader.reply(
                        calls(
                            tool_call(
                                "run_subagent",
                                "first",
                                task="Report",
                                agent_name="code",
                                background_after=budget,
                            ),
                        ),
                        answer("Parent free"),
                        run_child=run_child,
                    ),
                )
                if not detach and approval:
                    # The parent's call keeps waiting while the job asks for and receives its child's approval.
                    await decide()
                result = await asyncio.wait_for(driving, JOB_TEST_TIMEOUT)
                assert result.status is RunStatus.completed
                assert len(children) == 1
                child = children[0]
                first = _tool_message(result, "first")
                if detach:
                    handle = json.loads(first)
                    assert set(handle) == {"job_id", "subagent_id", "status", "tool", "summary", "summary_truncated"}
                    assert handle["job_id"] == child.delegation_id
                    assert handle["subagent_id"] == child.subagent_id
                    assert handle["tool"] == "code"
                    assert handle["status"] == "running"
                    assert not completed.is_set()
                    assert DelegationState.from_metadata(result.metadata).children == []
                    if human:
                        listed = json.loads(await JobTools(paths, identity).job("list"))
                        assert [item["status"] for item in listed if item["job_id"] == child.delegation_id] == [
                            "running",
                        ]
                    release.set()
                    if human:
                        # The newer reply that took the work over has started.
                        signal.clear()
                    if approval:
                        await decide()
                    status = "cancelled" if approval == "cancelled" else "completed"
                    await wait_for_status(runtime, child.delegation_id, status)
                    # Like the reply join, retrieve only once the job finished.
                    result = await asyncio.wait_for(
                        leader.reply(
                            calls(tool_call("job", "wait", action="wait", job_id=child.delegation_id)),
                            answer("Read"),
                            run_child=run_child,
                            run_id="wait-run",
                        ),
                        JOB_TEST_TIMEOUT,
                    )
                    first = _tool_message(result, "wait")
                if approval == "cancelled":
                    assert "cancelled" in first
                else:
                    assert "Exact child result" in first
                    assert child.subagent_id in first
                assert reports.effects == (["written"] if approval == "approved" else [])
                # The reply records the delegation before its job starts, and what the parent's call returned; the
                # child's own tools run unrecorded in that job.
                started, finished = recorder.records[:2]
                assert started == ("started", "run_subagent", {"task": "Report", "agent_name": "code"}, None)
                assert finished[:2] == ("finished", "run_subagent")
                returned = str(finished[3])
                if detach:
                    assert json.loads(returned)["job_id"] == child.delegation_id
                else:
                    assert ("cancelled" if approval == "cancelled" else "Exact child result") in returned
                assert "write_report" not in {record[1] for record in recorder.records}
                saved = await lookup(runtime, child.delegation_id, owner=identity, depth=0)
                assert saved.adapter["child"]["status"] == saved.status
        finally:
            release.set()
            await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["authored", "reloaded"])
async def test_a_parent_waits_for_its_childs_approval_no_longer_than_a_gated_call_would(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    """With the decision still out after the approval wait, the parent's call returns the child's handle and goes on.

    A child asks under the approval policy current when it pauses: a reload that made its call need approval, after
    the parent delegated under a policy that approved it, still asks.
    """
    paths = _runtime_paths(tmp_path)

    def build_config(action: Literal["auto_approve", "require_approval"]) -> Config:
        built = _config(child_tools=["reports"], rules=[ApprovalRuleConfig(match="write_report", action=action)])
        built.background_tool_jobs.approval_wait_timeout = 0.05
        return built

    config = build_config("auto_approve" if policy == "reloaded" else "require_approval")
    current = build_config("require_approval")
    identity = _identity("p")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = _JobApprovalCards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    agents = ScriptedAgents({"code": ScriptedModel([calls(tool_call("write_report", "w"))])})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)
    live = [config]
    real_child = ai.run_delegated_child_response

    async def run_child(child: DelegationChild, **kwargs: Any) -> str:  # noqa: ANN401
        # The configuration is reloaded while the child runs, after its job was admitted.
        live[0] = current
        return await real_child(child, **{**kwargs, "config": current})

    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=identity),
        config_provider=lambda: live[0],
    )
    with _registered_reports() as reports:
        try:
            with tool_runtime_context(context):
                result = await asyncio.wait_for(
                    leader.reply(
                        calls(tool_call("run_subagent", "first", task="Report", agent_name="code")),
                        answer("Parent free"),
                        run_child=run_child,
                    ),
                    JOB_TEST_TIMEOUT,
                )
            assert result.status is RunStatus.completed
            handle = json.loads(_tool_message(result, "first"))
            assert handle["status"] == "awaiting_approval"
            assert cards.requested == [(_approval_run_id(handle["job_id"]), "w", "write_report")]
            assert reports.effects == []
        finally:
            await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_resumed_child_runs_under_the_policy_current_when_it_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child resumed after its approvals is rebuilt under the current policy, so a rule a reload added gates it."""
    paths = _runtime_paths(tmp_path)
    write_rule = ApprovalRuleConfig(match="write_report", action="require_approval")
    delete_rule = ApprovalRuleConfig(match="delete_report", action="require_approval")
    start = _config(child_tools=["reports"], rules=[write_rule])
    start.background_tool_jobs.approval_wait_timeout = 30
    # Live config after a reload: delete_report now requires approval too.
    current = _config(child_tools=["reports"], rules=[write_rule, delete_rule])
    current.background_tool_jobs.approval_wait_timeout = 30
    identity = _identity("p")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(start, paths)
    register_background_runtime(paths, runtime)
    cards = _JobApprovalCards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    code = ScriptedModel(
        [calls(tool_call("write_report", "w")), calls(tool_call("delete_report", "d")), answer("Child done")],
    )
    agents = ScriptedAgents({"code": code})
    agents.install(monkeypatch)
    leader = DelegatingLeader(start, paths, identity, agents)
    live = [start]
    real_child = ai.run_delegated_child_response

    async def run_child(child: DelegationChild, **kwargs: Any) -> str:  # noqa: ANN401
        # The child starts under the authored policy; the operator reloads the configuration while it runs.
        try:
            return await real_child(child, **kwargs)
        finally:
            live[0] = current

    context = replace(
        _delegate_runtime_context(start, paths, execution_identity=identity),
        config_provider=lambda: live[0],
    )

    async def approve() -> None:
        await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
        cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    with _registered_reports() as reports:
        try:
            with tool_runtime_context(context):
                approving = asyncio.create_task(approve())
                result = await asyncio.wait_for(
                    leader.reply(
                        calls(tool_call("run_subagent", "first", task="Report", agent_name="code")),
                        answer("Parent done"),
                        run_child=run_child,
                    ),
                    JOB_TEST_TIMEOUT,
                )
                await approving
            assert result.status is RunStatus.completed
            assert "Child done" in _tool_message(result, "first")
            # The resumed child was built under the reloaded policy, so its newly gated call asked first.
            assert [card[2] for card in cards.requested] == ["write_report", "delete_report"]
            assert reports.effects == ["written", "deleted"]
        finally:
            await runtime.shutdown()


@pytest.mark.asyncio
async def test_native_wait_unavailable_job_returns_tool_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The native driver resolves generic lookup rejection instead of aborting its parent run."""
    paths = _runtime_paths(tmp_path)
    config = _config()
    identity = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)
    try:
        async with execution_resources():
            with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
                result = await asyncio.wait_for(
                    leader.reply(
                        calls(tool_call("job", "missing-call", action="wait", job_id="missing")),
                        answer("Job unavailable"),
                        run_child=_unused_child,
                    ),
                    JOB_TEST_TIMEOUT,
                )
        assert result.status is RunStatus.completed
        assert "No job with this ID is available to you in this conversation." in _tool_message(result, "missing-call")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_early_child_failure_retains_liveness_through_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery cannot claim a child between startup failure and durable terminal settlement."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    child = prepare_child_turn(
        "leader",
        "code",
        "task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )
    settling, release = asyncio.Event(), asyncio.Event()
    original_interrupt = delegation_execution.interrupt_child

    async def interrupt(
        retained: DelegationChild,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        reason: str,
        status: Literal["cancelled", "failed"] = "cancelled",
    ) -> None:
        assert retained is child
        settling.set()
        await release.wait()
        await original_interrupt(retained, config=config, runtime_paths=runtime_paths, reason=reason, status=status)

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "startup failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(delegation_execution, "interrupt_child", interrupt)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
        await start_child_turn(
            child,
            parent_run_id="parent-run",
            config=config,
            runtime_paths=paths,
            caller_execution_identity=owner,
        )
        pending = asyncio.create_task(
            delegation_execution._background_child_outcome(
                child,
                owner=owner,
                run_child=run_child,
                config=config,
                runtime_paths=paths,
                refresh_scheduler=None,
            ),
        )
        try:
            await asyncio.wait_for(settling.wait(), JOB_TEST_TIMEOUT)
            with subagent_recovery_lock(child.subagent_id, paths) as acquired:
                assert not acquired, "Failure settlement released its exact live child too early"
        finally:
            release.set()
            outcome = await pending
    assert outcome.status == "failed"
    assert "startup failed" in outcome.result
    retained = await load_retained_subagent_turn(child, paths)
    assert retained.status == "failed"
    assert retained.delegation_id == child.delegation_id
    with subagent_recovery_lock(child.subagent_id, paths) as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_inline_child_failure_retains_liveness_through_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreground child that fails stays claimed until its failure is durably settled."""
    paths = _runtime_paths(tmp_path)
    config = _config(jobs=False)
    owner = _identity()
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, owner, agents)
    children: list[DelegationChild] = []
    settling, release = asyncio.Event(), asyncio.Event()
    original_interrupt = delegation_execution.interrupt_child

    async def interrupt(
        retained: DelegationChild,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        reason: str,
        status: Literal["cancelled", "failed"] = "cancelled",
    ) -> None:
        settling.set()
        await release.wait()
        await original_interrupt(retained, config=config, runtime_paths=runtime_paths, reason=reason, status=status)

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        children.append(child)
        msg = "startup failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(delegation_execution, "interrupt_child", interrupt)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
        paused = await leader.turn(
            calls(tool_call("run_subagent", "failing", agent_name="code", task="Research")),
            answer("Parent done"),
        )
        pending = asyncio.create_task(leader.drive(paused, run_child))
        try:
            await asyncio.wait_for(settling.wait(), JOB_TEST_TIMEOUT)
            with subagent_recovery_lock(children[0].subagent_id, paths) as acquired:
                assert not acquired, "Failure settlement released its exact live child too early"
        finally:
            release.set()
        response = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
    assert response.status is RunStatus.completed
    with subagent_recovery_lock(children[0].subagent_id, paths) as acquired:
        assert acquired


_BACKGROUND_PARENT = _identity()


async def _drive_background_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: ToolJobRuntime,
    run_child: Callable[..., Awaitable[str]],
    *,
    while_waiting: Callable[[], Awaitable[None]] | None = None,
) -> RunRecord:
    """Drive one parent delegation whose child runs as a managed job, acting once the child starts."""
    paths = _runtime_paths(tmp_path)
    config = _config(jobs=False)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, _BACKGROUND_PARENT, agents)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_BACKGROUND_PARENT)):
            paused = await leader.turn(
                calls(tool_call("run_subagent", "waiting", agent_name="code", task="Research")),
                answer("Parent done"),
            )
            pending = asyncio.create_task(leader.drive(paused, run_child))
            if while_waiting is not None:
                await while_waiting()
            return await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
    finally:
        await runtime.shutdown()


class _BlockingChild:
    """A child runner that stays running until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.child: DelegationChild | None = None

    async def __call__(self, child: DelegationChild, **_kwargs: object) -> str:
        self.child = child
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def running(self) -> DelegationChild:
        await asyncio.wait_for(self.started.wait(), JOB_TEST_TIMEOUT)
        assert self.child is not None
        return self.child


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_shutdown_interrupts_a_running_child_like_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: bool,
) -> None:
    """Only a cancellation request cancels a running child; shutdown records the restart interruption."""
    runtime = await tool_job_runtime(tmp_path)
    child = _BlockingChild()

    async def stop() -> None:
        running = await child.running()
        if shutdown:
            await runtime.quiesce()
        else:
            await cancel_settled(runtime, running.delegation_id, owner=_BACKGROUND_PARENT, depth=0)

    await _drive_background_child(tmp_path, monkeypatch, runtime, child, while_waiting=stop)
    (job,) = (await saved_jobs(tmp_path)).values()
    assert (job.status, job.result) == (
        ("failed", "Subagent turn was interrupted by a restart. Send a follow-up to continue its history.")
        if shutdown
        else ("cancelled", "Delegation cancelled.")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "shutdown", "teardown", "raised"])
async def test_cancelled_child_inside_a_stopping_job_records_why_it_stopped(tmp_path: Path, stop: str) -> None:
    """Every child a job owns, including one running inline below it, records a shutdown or teardown as a restart.

    An operation that raises cancellation itself, with no request and no task cancellation, is cancelled.
    """
    control = JobControl()
    if stop in {"cancel", "shutdown"}:
        control.cancel(shutdown=stop == "shutdown")
    interrupt = AsyncMock()

    async def unwind() -> None:
        await delegation_recovery.interrupt_stopped_child(
            job_child(),
            config=Config(),
            runtime_paths=_runtime_paths(tmp_path),
        )

    async def torn_down() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await unwind()
            raise

    with job_control_context(control), patch.object(delegation_recovery, "interrupt_child", new=interrupt):
        if stop == "teardown":
            task = asyncio.create_task(torn_down())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await unwind()
    reason = interrupt.await_args.kwargs["reason"]
    assert (reason, interrupt.await_args.kwargs.get("status", "cancelled")) == (
        ("Subagent turn was interrupted by a restart. Send a follow-up to continue its history.", "failed")
        if stop in {"shutdown", "teardown"}
        else ("Delegation cancelled.", "cancelled")
    )


def test_shutdown_during_a_cancellation_keeps_it_a_cancellation() -> None:
    """The first stop cause stands, so shutdown cannot relabel an explicit cancellation that is still unwinding."""
    control = JobControl()
    control.cancel()
    control.cancel(shutdown=True)
    assert (control.cancelled, control.shutdown) == (True, False)


@pytest.mark.asyncio
async def test_revoked_child_wait_becomes_the_parent_call_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing access while the parent waits on its background child ends the call, not the parent's reply."""
    allowed = True
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    child = _BlockingChild()

    async def revoke() -> None:
        nonlocal allowed
        await child.running()
        allowed = False
        await runtime.cancel_revoked(denied=lambda _job: True)

    response = await _drive_background_child(tmp_path, monkeypatch, runtime, child, while_waiting=revoke)
    assert response.status is RunStatus.completed
    result = next(tool.result for tool in response.tools or () if tool.tool_call_id == "waiting")
    assert "not available" in str(result)
    state = DelegationState.from_metadata(response.metadata)
    assert state.children == []
    assert all(hook.after_called for hook in state.hooks.values())


@pytest.mark.asyncio
async def test_waiting_parent_leaves_the_child_liveness_claim_to_its_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent claim across the wait would block cancel cleanup of a recovered child."""
    parent_claims = 0
    parent_idle = asyncio.Event()
    parent_idle.set()
    original_liveness = delegation_execution.subagent_liveness

    @asynccontextmanager
    async def liveness(child: DelegationChild, runtime_paths: RuntimePaths) -> AsyncIterator[None]:
        nonlocal parent_claims
        parent = not job_owns_execution()
        async with original_liveness(child, runtime_paths):
            if parent:
                parent_claims += 1
                parent_idle.clear()
            try:
                yield
            finally:
                if parent:
                    parent_claims -= 1
                    if not parent_claims:
                        parent_idle.set()

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        # The child can finish only once the waiting parent no longer holds its claim.
        await asyncio.wait_for(parent_idle.wait(), JOB_TEST_TIMEOUT)
        return "Child finished"

    monkeypatch.setattr(delegation_execution, "subagent_liveness", liveness)
    response = await _drive_background_child(tmp_path, monkeypatch, await tool_job_runtime(tmp_path), run_child)
    assert response.status is RunStatus.completed


@pytest.mark.asyncio
async def test_rejected_background_start_settles_the_child_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start refused before any job owns the child finishes its audit record like any child failure."""
    ran: list[DelegationChild] = []

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        ran.append(child)
        return "never"

    held_during_settlement: list[bool] = []
    original_interrupt = delegation_execution.interrupt_child

    async def interrupt(child: DelegationChild, **kwargs: Any) -> None:  # noqa: ANN401
        with subagent_recovery_lock(child.subagent_id, _runtime_paths(tmp_path)) as acquired:
            held_during_settlement.append(not acquired)
        await original_interrupt(child, **kwargs)

    monkeypatch.setattr(delegation_execution, "interrupt_child", interrupt)
    response = await _drive_background_child(
        tmp_path,
        monkeypatch,
        await tool_job_runtime(tmp_path, authorize=lambda _job: False),
        run_child,
    )
    assert ran == []
    assert held_during_settlement == [True], "Recovery could take the child mid-failure"
    result = str(next(tool.result for tool in response.tools or () if tool.tool_call_id == "waiting"))
    assert "failed: Tool job is not available" in result
    records = [json.loads(path.read_text()) for path in tmp_path.rglob("delegations/**/run.json")]
    assert [record["status"] for record in records] == ["failed"]
    state = DelegationState.from_metadata(response.metadata)
    assert all(hook.after_called for hook in state.hooks.values())


@pytest.mark.asyncio
async def test_unreadable_background_result_still_runs_the_after_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-access failure after the job owns the child still closes the delegation's after hook."""
    after_errors: list[BaseException | None] = []
    original_after = delegation_execution.after_delegation

    async def after(*args: object, error: BaseException | None = None, **kwargs: object) -> None:
        after_errors.append(error)
        await original_after(*args, error=error, **kwargs)

    async def unreadable(*_args: object, **_kwargs: object) -> str:
        msg = "result unreadable"
        raise RuntimeError(msg)

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        return "Child finished"

    monkeypatch.setattr(delegation_execution, "after_delegation", after)
    monkeypatch.setattr(delegation_execution, "delegation_result", unreadable)
    with pytest.raises(RuntimeError, match="result unreadable"):
        await _drive_background_child(tmp_path, monkeypatch, await tool_job_runtime(tmp_path), run_child)
    assert [type(error) for error in after_errors] == [RuntimeError]


@pytest.mark.asyncio
async def test_child_failure_remains_primary_when_native_interruption_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed native cleanup reports uncertainty without replacing or falsely settling the child."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    child = prepare_child_turn(
        "leader",
        "code",
        "task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "primary execution failed"
        raise RuntimeError(msg)

    async def interrupt(*_args: object, **_kwargs: object) -> None:
        msg = "native cleanup unavailable"
        raise OSError(msg)

    finish = AsyncMock(side_effect=AssertionError("Unsettled native cleanup cannot be reported as terminal"))
    monkeypatch.setattr(delegation_execution, "interrupt_child", interrupt)
    monkeypatch.setattr(delegation_execution, "finish_child_turn", finish)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
        await start_child_turn(
            child,
            parent_run_id="parent-run",
            config=config,
            runtime_paths=paths,
            caller_execution_identity=owner,
        )
        outcome = await delegation_execution._background_child_outcome(
            child,
            owner=owner,
            run_child=run_child,
            config=config,
            runtime_paths=paths,
            refresh_scheduler=None,
        )

    assert outcome.status == "failed"
    assert "primary execution failed" in (outcome.result or "")
    assert "cleanup" in (outcome.result or "").lower()
    assert "native cleanup unavailable" in (outcome.result or "")
    finish.assert_not_awaited()
    retained = await load_retained_subagent_turn(child, paths)
    assert retained.status == "running"
    with subagent_recovery_lock(child.subagent_id, paths) as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_child_failure_remains_primary_when_terminal_receipt_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt failure cannot erase the execution error or overwrite terminal native evidence."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    child = prepare_child_turn(
        "leader",
        "code",
        "task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "primary execution failed"
        raise RuntimeError(msg)

    async def fail_receipt(*_args: object, **_kwargs: object) -> str:
        msg = "terminal receipt unavailable"
        raise OSError(msg)

    monkeypatch.setattr(delegation_execution, "finish_child_turn", fail_receipt)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
        await start_child_turn(
            child,
            parent_run_id="parent-run",
            config=config,
            runtime_paths=paths,
            caller_execution_identity=owner,
        )
        outcome = await delegation_execution._background_child_outcome(
            child,
            owner=owner,
            run_child=run_child,
            config=config,
            runtime_paths=paths,
            refresh_scheduler=None,
        )

    assert outcome.status == "failed"
    assert "primary execution failed" in (outcome.result or "")
    assert "settlement" in (outcome.result or "").lower()
    assert "terminal receipt unavailable" in (outcome.result or "")
    retained = await load_retained_subagent_turn(child, paths)
    assert retained.status == "failed"
    assert retained.result == "primary execution failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_event_id", [None, "$original-request"])
async def test_native_job_source_survives_restart(
    tmp_path: Path,
    source_event_id: str | None,
) -> None:
    """A native child keeps the human source that accepted it through settlement and restart."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        membership_turn_id=source_event_id,
    )
    child = prepare_child_turn(
        "leader",
        "code",
        "task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )
    runtime = await tool_job_runtime(tmp_path)

    async def completed() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "finished once"
        return BackgroundOutcome("completed", child.result)

    try:
        with tool_runtime_context(context):
            job = await start_delegation_job(runtime, child, owner=owner, operation=completed)
        waited = await runtime.wait(job.job_id, owner=owner, depth=0)
        assert waited.job.source_event_id == source_event_id
        assert waited.job.owner == owner
        assert waited.job.result == "finished once"
        await runtime.release_wait(job.job_id, waited.claim)
    finally:
        await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        saved = await lookup(restored, child.delegation_id, owner=owner, depth=0)
        assert saved.source_event_id == source_event_id
        assert saved.owner == owner
        assert delegation_child(saved).run_id == child.run_id
        assert saved.status == "completed"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1])
async def test_only_a_top_level_delegate_offers_background_waits(tmp_path: Path, depth: int) -> None:
    """A nested subagent's delegate calls always run inline, so its guidance offers no background wait."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            "code": AgentConfig(display_name="Code"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    config.background_tool_jobs.enabled = True
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    try:
        delegate = DelegateTools("leader", ["code"], paths, config, execution_identity=owner, delegation_depth=depth)
        description = delegate.async_functions["run_subagent"].description or ""
        offered = "background_after" in description
        instructed = config.get_prompt("DELEGATE_BACKGROUND_JOB_INSTRUCTIONS") in (delegate.instructions or "")
        assert (offered, instructed) == (depth == 0, depth == 0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_childs_outcome_counts_as_read_only_once_its_parent_run_saves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent whose run never saves the child's result leaves the outcome for a later reply to collect."""

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        return "Child answer"

    await _drive_background_child(tmp_path, monkeypatch, await tool_job_runtime(tmp_path), run_child)
    [job] = (await saved_jobs(tmp_path)).values()
    assert job.status in TERMINAL_STATUSES
    assert not job.consumed
