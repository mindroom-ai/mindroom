"""Background subagent jobs retain exact execution and approval ownership.

The leader runs as ``DelegatingLeader`` builds it: a reply's agent on the owned loop whose subagent calls run as
managed jobs, or, with background jobs off or the ``delegate`` toolkit excluded, pause for the native driver, which
runs their children and continues through the production resume. A child that needs approvals runs as a real child
turn of ``code``.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import ai
from mindroom.agent_loop.model_loop import EMPTY_REQUEST
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.agents import create_agent
from mindroom.approval import manager as approval_manager
from mindroom.approval.auto_approve_windows import start_auto_approve
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools import delegate as delegate_toolkit
from mindroom.custom_tools.job import JobTools
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation import recovery as delegation_recovery
from mindroom.delegation.background import delegation_child
from mindroom.delegation.job_approvals import request_child_approvals
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.sessions import SUBAGENT_BUSY, load_retained_subagent_turn, subagent_recovery_lock
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import JOB_ENDED_REASON, BackgroundApprovalDecision, tool_job_run_id
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.tool_jobs.control import (
    JobControl,
    job_control_context,
    job_owns_execution,
    pending_human_messages_context,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate, ToolOutcome
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.agent_loop.helpers import collect, spec, toolkit
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, respond
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids, persist_entity_accounts
from tests.scripted_agent_helpers import registered_toolkit
from tests.tool_job_helpers import (
    DELEGATING_REQUESTER,
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    JobApprovalCards,
    SpanRecorder,
    admit_delegation_spec,
    answer_of,
    cancel_settled,
    delegating_config,
    intercept_job_saves,
    job_card_decision,
    job_child,
    lookup,
    saved_jobs,
    start_delegation_job,
    subagent_job_id,
    tool_job_runtime,
    wait_for_status,
)
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.agent_modes import AgentMode
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, DelegationHookState
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime


def _identity(session_id: str = "parent") -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", "leader", DELEGATING_REQUESTER, "!room:example.org", None, None, session_id)


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
    """Register the ``reports`` toolkit for one test and restore the built-in tools afterwards."""
    _ReportTools.effects.clear()
    with registered_toolkit("reports", _ReportTools):
        yield _ReportTools


async def _unused_child(*_args: object, **_kwargs: object) -> str:
    msg = "This delegation must never start a child"
    raise AssertionError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize(
    ("tool_name", "budget", "depth", "excluded"),
    [
        ("run_subagent", 0, 1, False),
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
    config = delegating_config()
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
        assert rejected.is_error
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
@pytest.mark.parametrize(("tool_name", "budget"), [("run_subagent", "bad"), ("continue_subagent", -1)])
async def test_an_unusable_wait_budget_fails_a_subagent_call_before_any_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    budget: object,
) -> None:
    """A subagent call whose wait budget is unusable fails as a call the model can correct; no job or child starts."""
    paths = _runtime_paths(tmp_path)
    config = delegating_config()
    owner = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, owner, agents)
    arguments = {"task": "must-not-run", "agent_name": "code", "background_after": budget}
    if tool_name == "continue_subagent":
        arguments = {"subagent_id": "missing", "message": "must-not-run", "background_after": budget}
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            response = await asyncio.wait_for(
                leader.reply(calls(tool_call(tool_name, "invalid", **arguments)), answer("corrected")),
                JOB_TEST_TIMEOUT,
            )
        assert response.status is RunStatus.completed
        assert response.content == "corrected"
        rejected = next(tool for tool in response.tools or [] if tool.tool_call_id == "invalid")
        assert rejected.is_error
        assert "background_after" in str(rejected.result)
        assert rejected.tool_args == arguments
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
    config = delegating_config()
    owner = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    written, executing, release_writer = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run_forever() -> None:
        executing.set()
        await asyncio.Event().wait()

    agents = ScriptedAgents({"code": ScriptedModel([[Await(run_forever)]])})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, owner, agents)

    async def blocked_writer(_job: dict[str, object], _payload: object) -> None:
        written.set()
        await release_writer.wait()

    recorder = SpanRecorder(stopped=False)
    stop = ending != "restart"
    try:
        with (
            tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)),
            recording_tool_calls(recorder),
        ):
            intercept_job_saves(monkeypatch, before=blocked_writer)
            replying = asyncio.create_task(
                leader.reply(calls(tool_call("run_subagent", "call", task="Research", agent_name="code"))),
            )
            await asyncio.wait_for(written.wait(), JOB_TEST_TIMEOUT)
            # A Stop or deletion commits while the job is admitted, after the reply recorded the call.
            recorder.stopped = stop
            if ending == "stop":
                request_task_cancel(replying, cancel_source="user_stop")
            else:
                replying.cancel()
            release_writer.set()
            with pytest.raises(asyncio.CancelledError):
                await replying
            [job] = await runtime.list_jobs(owner=owner, depth=0)
            assert (job.kind, job.tool_name) == ("delegation", "code")
            await asyncio.wait_for(executing.wait(), JOB_TEST_TIMEOUT)
            assert delegation_child(await lookup(runtime, job.job_id, owner=owner, depth=0)).status == "running"
    finally:
        release_writer.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("detach", "human"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(
    ("approval", "exclude_after_acceptance"),
    [
        (None, False),
        *((approval, exclude) for approval in ("approved", "denied", "cancelled") for exclude in (False, True)),
    ],
)
async def test_a_background_subagent_runs_its_child_once(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detach: bool,
    human: bool,
    approval: Literal["approved", "denied", "cancelled"] | None,
    exclude_after_acceptance: bool,
) -> None:
    """A released parent cannot cancel the child, the job owns its approval cards, and later waits read its result."""
    paths = _runtime_paths(tmp_path)
    config = delegating_config(
        child_tools=["reports"],
        rules=[ApprovalRuleConfig(match="write_report", action="require_approval")],
    )
    identity = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future(), runtime.journal)
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    release = asyncio.Event()
    signal = asyncio.Event()

    async def start() -> None:
        """The child's first model request: a newer message arrives, and a detached child waits to be released."""
        if human:
            signal.set()
        if detach:
            await release.wait()

    code = ScriptedModel(
        [
            [
                Await(start),
                *(calls(tool_call("write_report", "write-once")) if approval else answer("Exact child result")),
            ],
            *([answer("Exact child result")] if approval else []),
        ],
    )
    agents = ScriptedAgents({"code": code})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)

    def the_job() -> BackgroundJob:
        [job] = runtime.retained_jobs()
        return job

    async def decide() -> None:
        """Answer the job's card as the requester would, while a parent may still be waiting."""
        await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
        job_id = the_job().job_id
        await wait_for_status(runtime, job_id, "awaiting_approval")
        assert cards.requested == [(tool_job_run_id(job_id), "write-once", "write_report")]
        assert reports.effects == []
        if exclude_after_acceptance:
            config.background_tool_jobs.exclude_toolkits.append("delegate")
        if approval == "cancelled":
            assert '"status": "cancel_requested"' in str(await JobTools(paths).job("cancel", job_id))
            await wait_for_status(runtime, job_id, "cancelled")
            cancelled = await read_child_run(delegation_child(the_job()), config, paths)
            assert cancelled is not None
            assert cancelled.status is RunStatus.cancelled
            assert await job_card_decision(
                runtime.journal,
                tool_job_run_id(job_id),
                "write-once",
            ) == BackgroundApprovalDecision("denied", JOB_ENDED_REASON)
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
                    ),
                )
                if not detach and approval:
                    # The parent's call keeps waiting while the job asks for and receives its child's approval.
                    await decide()
                result = await asyncio.wait_for(driving, JOB_TEST_TIMEOUT)
                assert result.status is RunStatus.completed
                job = the_job()
                child = delegation_child(job)
                first = str(answer_of(result, "first").content)
                if detach:
                    handle = json.loads(first)
                    assert set(handle) == {"job_id", "subagent_id", "status", "tool", "summary", "summary_truncated"}
                    assert handle["job_id"] == job.job_id
                    assert handle["subagent_id"] == child.subagent_id
                    assert handle["tool"] == "code"
                    assert handle["status"] == "running"
                    assert job.status == "running"
                    assert DelegationState.from_metadata(result.metadata).children == []
                    if human:
                        listed = json.loads(await JobTools(paths).job("list"))
                        assert [item["status"] for item in listed if item["job_id"] == job.job_id] == ["running"]
                    release.set()
                    if human:
                        # The newer reply that took the work over has started.
                        signal.clear()
                    if approval:
                        await decide()
                    status = "cancelled" if approval == "cancelled" else "completed"
                    await wait_for_status(runtime, job.job_id, status)
                    # Like the reply join, retrieve only once the job finished.
                    result = await asyncio.wait_for(
                        leader.reply(
                            calls(tool_call("job", "wait", action="wait", job_id=job.job_id)),
                            answer("Read"),
                            run_id="wait-run",
                        ),
                        JOB_TEST_TIMEOUT,
                    )
                    first = str(answer_of(result, "wait").content)
                if approval == "cancelled":
                    assert "cancelled" in first
                else:
                    assert "Exact child result" in first
                    assert child.subagent_id in first
                # The child started once, in its job; a later wait read the job without running it again.
                assert len(runtime.retained_jobs()) == 1
                assert len(code.requests) == (1 if approval in {None, "cancelled"} else 2)
                assert reports.effects == (["written"] if approval == "approved" else [])
                if approval == "denied":
                    # The child's model is answered with the requester's reason for the denial.
                    assert [
                        (message.tool_call_id, str(message.content))
                        for message in code.requests[-1].messages
                        if message.role == "tool"
                    ] == [("write-once", "Not now")]
                # The reply records the delegation before its job starts, and what the parent's call returned; the
                # child's own tools run unrecorded in that job.
                started, finished = recorder.records[:2]
                assert started == ("started", "run_subagent", {"task": "Report", "agent_name": "code"}, None)
                assert finished[:2] == ("finished", "run_subagent")
                returned = finished[3]
                if detach:
                    assert json.loads(str(returned))["job_id"] == job.job_id
                elif approval == "cancelled":
                    # A child that did not complete fails its parent's call, which the reply records as failed.
                    assert isinstance(returned, ToolOutcome)
                    assert returned.is_error
                    assert "cancelled" in returned.content
                else:
                    assert "Exact child result" in str(returned)
                assert "write_report" not in {record[1] for record in recorder.records}
                saved = await lookup(runtime, job.job_id, owner=identity, depth=0)
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
        built = delegating_config(
            child_tools=["reports"],
            rules=[ApprovalRuleConfig(match="write_report", action=action)],
        )
        built.background_tool_jobs.approval_wait_timeout = 0.05
        return built

    config = build_config("auto_approve" if policy == "reloaded" else "require_approval")
    current = build_config("require_approval")
    identity = _identity("p")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    agents = ScriptedAgents({"code": ScriptedModel([calls(tool_call("write_report", "w"))])})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)
    live = [config]

    def reload(_job: dict[str, object], _payload: object) -> None:
        # The configuration is reloaded once the subagent's job was admitted, before its child runs.
        live[0] = current

    intercept_job_saves(monkeypatch, after=reload)
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
                    ),
                    JOB_TEST_TIMEOUT,
                )
            assert result.status is RunStatus.completed
            handle = json.loads(str(answer_of(result, "first").content))
            assert handle["status"] == "awaiting_approval"
            assert cards.requested == [(tool_job_run_id(handle["job_id"]), "w", "write_report")]
            assert reports.effects == []
        finally:
            await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("window_thread", ["$thread", "$other"])
async def test_a_childs_gated_call_skips_its_card_only_inside_the_requesters_auto_approve_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window_thread: str,
) -> None:
    """A delegated child asks in its job's conversation, so the requester's window there approves its call."""
    paths = _runtime_paths(tmp_path)
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        "$thread",
        "$thread",
        "p",
    )
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future())
    cards.decision.set_result(BackgroundApprovalDecision("denied", "No."))
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    start_auto_approve(
        paths,
        room_id="!room:example.org",
        thread_id=window_thread,
        requester_id="@alice:example.org",
        duration=timedelta(minutes=5),
    )

    def write_report() -> str:
        """Write the report."""
        return "Report written"

    child = job_child()
    model = ScriptedModel([calls(tool_call("write_report", "w"))])
    _, response = await collect(
        run_agent(
            spec(model, toolkit(write_report, name="file", gate=ToolGate.CONFIRM)),
            [Message(role="user", content="Report")],
            ctx=RunContext(run_id=child.run_id, session=UnstoredSession(child.session_id)),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    await start_delegation_job(runtime, child, owner=identity, operation=operation)
    try:
        decided = await request_child_approvals(
            child,
            response,
            {(child.child_agent_name, "write_report"): "file"},
            runtime=runtime,
            job_id=child.delegation_id,
            owner=identity,
            config=config,
            runtime_paths=paths,
        )
    finally:
        finish.set()
        await runtime.shutdown()

    inside = window_thread == "$thread"
    assert {call.tool_call_id: call.approved for call in decided} == {"w": inside}
    assert len(cards.requested) == (0 if inside else 1)


@pytest.mark.asyncio
async def test_a_resumed_child_runs_under_the_policy_current_when_it_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child resumed after its approvals is rebuilt under the current policy, so a rule a reload added gates it."""
    paths = _runtime_paths(tmp_path)
    write_rule = ApprovalRuleConfig(match="write_report", action="require_approval")
    delete_rule = ApprovalRuleConfig(match="delete_report", action="require_approval")
    start = delegating_config(child_tools=["reports"], rules=[write_rule])
    start.background_tool_jobs.approval_wait_timeout = 30
    # Live config after a reload: delete_report now requires approval too.
    current = delegating_config(child_tools=["reports"], rules=[write_rule, delete_rule])
    current.background_tool_jobs.approval_wait_timeout = 30
    identity = _identity("p")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(start, paths)
    register_background_runtime(paths, runtime)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future())
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    code = ScriptedModel(
        [calls(tool_call("write_report", "w")), calls(tool_call("delete_report", "d")), answer("Child done")],
    )
    agents = ScriptedAgents({"code": code})
    agents.install(monkeypatch)
    leader = DelegatingLeader(start, paths, identity, agents)
    live = [start]

    def reload(job: dict[str, object], _payload: object) -> None:
        # The child starts under the authored policy; the operator reloads the configuration once it pauses.
        if job["status"] == "awaiting_approval":
            live[0] = current

    intercept_job_saves(monkeypatch, before=reload)
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
                    ),
                    JOB_TEST_TIMEOUT,
                )
                await approving
            assert result.status is RunStatus.completed
            assert "Child done" in str(answer_of(result, "first").content)
            # The resumed child was built under the reloaded policy, so its newly gated call asked first.
            assert [card[2] for card in cards.requested] == ["write_report", "delete_report"]
            assert reports.effects == ["written", "deleted"]
        finally:
            await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_wait_on_an_unavailable_job_answers_its_call_with_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``job`` wait for an unknown ID answers its call with the refusal instead of aborting the reply's run."""
    paths = _runtime_paths(tmp_path)
    config = delegating_config()
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
                    ),
                    JOB_TEST_TIMEOUT,
                )
        assert result.status is RunStatus.completed
        assert "No job with this ID is available to you in this conversation." in str(
            answer_of(result, "missing-call").content,
        )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_delegated_child_s_job_is_owned_by_the_agent_that_delegated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegated child's job, started from a Matrix reply, saves this exact owner as the call's managed job.

    The child's own calls run at depth one and never become jobs; the job is the child itself, under the ID of the
    call that started it, as every managed call's job is.
    """
    paths = _runtime_paths(tmp_path)
    config = delegating_config()
    entity_ids(config, paths)
    # The identity a bot gives its own reply in a thread, as the runtime context it binds names it.
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        DELEGATING_REQUESTER,
        "!room:example.org",
        "$thread:example.org",
        "$thread:example.org",
        "!room:example.org:$thread:example.org",
        transport_agent_name="leader",
    )
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    leader = ScriptedModel(
        [calls(tool_call("run_subagent", "call-child", agent_name="code", task="Research")), answer("Done")],
    )
    ScriptedAgents({"leader": leader, "code": ScriptedModel([answer("Child done")])}).install(monkeypatch)
    try:
        async with execution_resources():
            await respond("leader", "Delegate", config=config, runtime_paths=paths, identity=identity, run_id="run-1")
    finally:
        await runtime.shutdown()

    [job] = (await saved_jobs(paths.storage_root)).values()
    assert (job.kind, job.tool_name, job.status) == ("delegation", "code", "completed")
    assert asdict(job.owner) == {
        "channel": "matrix",
        "agent_name": "leader",
        "requester_id": DELEGATING_REQUESTER,
        "room_id": "!room:example.org",
        "thread_id": "$thread:example.org",
        "resolved_thread_id": "$thread:example.org",
        "session_id": "!room:example.org:$thread:example.org",
        "tenant_id": None,
        "account_id": None,
        "transport_agent_name": "leader",
    }
    child = job.adapter["child"]
    assert (child["parent_tool_call_id"], child["caller_agent_name"]) == ("call-child", "leader")
    assert re.fullmatch(r"[0-9a-f]{64}", job.job_id)


def _prepared_child(paths: RuntimePaths) -> tuple[Config, ToolExecutionIdentity, DelegationChild]:
    """Return a config where ``leader`` may run ``code``, the leader's owner, and a fresh child turn of ``code``."""
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
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )
    return config, owner, child


async def _start_job_body(
    runtime: ToolJobRuntime,
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    paths: RuntimePaths,
    run_child: Callable[..., Awaitable[str]],
) -> None:
    """Admit the job a subagent call that runs ``child`` with ``run_child`` runs as, under the child's own ID."""
    spec = delegation_execution.delegation_call_job(
        child,
        job_id=child.delegation_id,
        runtime=runtime,
        owner=owner,
        parent_run_id="parent-run",
        tool_name="run_subagent",
        arguments={"agent_name": child.child_agent_name, "task": child.task},
        run_child=run_child,
        config=config,
        runtime_paths=paths,
        refresh_scheduler=None,
    )
    assert isinstance(spec, JobSpec), spec
    job, claim = await admit_delegation_spec(runtime, child, spec, owner=owner)
    await runtime.release_wait(job.job_id, claim)


async def _ended(runtime: ToolJobRuntime, child: DelegationChild, owner: ToolExecutionIdentity) -> BackgroundJob:
    """Wait for the job of ``child`` to end and return it."""
    waited = await asyncio.wait_for(runtime.wait(child.delegation_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
    await runtime.release_wait(child.delegation_id, waited.claim)
    return waited.job


@pytest.mark.asyncio
async def test_early_child_failure_retains_liveness_through_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery cannot claim a child between startup failure and durable terminal settlement."""
    paths = _runtime_paths(tmp_path)
    config, owner, child = _prepared_child(paths)
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
    runtime = await tool_job_runtime(tmp_path)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            await _start_job_body(runtime, child, owner=owner, config=config, paths=paths, run_child=run_child)
            try:
                await asyncio.wait_for(settling.wait(), JOB_TEST_TIMEOUT)
                with subagent_recovery_lock(child.subagent_id, paths) as acquired:
                    assert not acquired, "Failure settlement released its exact live child too early"
            finally:
                release.set()
            outcome = await _ended(runtime, child, owner)
    finally:
        await runtime.shutdown()
    assert outcome.status == "failed"
    assert "startup failed" in (outcome.result or "")
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
    config = delegating_config(jobs=False)
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
    child_model: ScriptedModel | None = None,
    recorder: SpanRecorder | None = None,
) -> RunRecord:
    """Drive one parent delegation whose child runs as a managed job, acting once the child starts.

    ``child_model`` answers a child that ``run_child`` runs as a real child turn; ``recorder`` records the reply's
    tool calls.
    """
    paths = _runtime_paths(tmp_path)
    config = delegating_config()
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({} if child_model is None else {"code": child_model})
    agents.install(monkeypatch)
    monkeypatch.setattr(delegate_toolkit, "run_delegated_child_response", run_child)
    leader = DelegatingLeader(config, paths, _BACKGROUND_PARENT, agents)
    try:
        with (
            tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_BACKGROUND_PARENT)),
            recording_tool_calls(recorder),
        ):
            pending = asyncio.create_task(
                leader.reply(
                    calls(tool_call("run_subagent", "waiting", agent_name="code", task="Research")),
                    answer("Parent done"),
                ),
            )
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
            await cancel_settled(runtime, subagent_job_id(runtime, running), owner=_BACKGROUND_PARENT, depth=0)

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
    """Losing access while the parent waits on its background child fails the call, not the parent's reply.

    The call answers as failed, as an ordinary job's call does when its wait is refused, and the reply records it so.
    """
    allowed = True
    runtime = await tool_job_runtime(tmp_path, grant=lambda _job: "allowed" if allowed else "denied")
    child = _BlockingChild()

    async def revoke() -> None:
        nonlocal allowed
        await child.running()
        allowed = False
        await runtime.cancel_revoked()

    recorder = SpanRecorder(stopped=False)
    response = await _drive_background_child(
        tmp_path,
        monkeypatch,
        runtime,
        child,
        while_waiting=revoke,
        recorder=recorder,
    )
    assert response.status is RunStatus.completed
    answered = answer_of(response, "waiting")
    assert answered.content == "Tool job is not available in this conversation."
    assert answered.tool_call_error is True
    assert recorder.records[-1] == (
        "finished",
        "run_subagent",
        {"agent_name": "code", "task": "Research"},
        ToolOutcome(content="Tool job is not available in this conversation.", is_error=True),
    )
    assert DelegationState.from_metadata(response.metadata).children == []


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
async def test_a_refused_background_start_fails_the_call_without_starting_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start the job runtime refuses fails the parent's call as any managed call's does; no child or record starts.

    The reply's record of the call is finished once, failed, with the text the parent reads.
    """
    ran: list[DelegationChild] = []

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        ran.append(child)
        return "never"

    recorder = SpanRecorder(stopped=False)
    response = await _drive_background_child(
        tmp_path,
        monkeypatch,
        await tool_job_runtime(tmp_path, grant=lambda _job: "denied"),
        run_child,
        recorder=recorder,
    )
    assert ran == []
    result = str(next(tool.result for tool in response.tools or () if tool.tool_call_id == "waiting"))
    assert result == "Tool job is not available in this conversation."
    arguments = {"agent_name": "code", "task": "Research"}
    assert recorder.records == [
        ("started", "run_subagent", arguments, None),
        ("finished", "run_subagent", arguments, ToolOutcome(content=result, is_error=True)),
    ]
    assert list(tmp_path.rglob("delegations/**/run.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_a_child_that_fails_without_error_text_tells_its_parent_what_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    background: bool,
) -> None:
    """A child that raises an exception without text, such as a bare TimeoutError, reports the exception's type."""

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        raise TimeoutError

    if background:
        response = await _drive_background_child(tmp_path, monkeypatch, await tool_job_runtime(tmp_path), run_child)
    else:
        response = await _drive_inline_child(tmp_path, monkeypatch, run_child)
    result = str(next(tool.result for tool in response.tools or () if tool.tool_call_id == "waiting"))
    assert result.startswith("Delegation to 'code' failed: TimeoutError\n\n")
    [record] = [json.loads(path.read_text()) for path in tmp_path.rglob("delegations/**/run.json")]
    assert (record["status"], record["error"]) == ("failed", "TimeoutError")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("background", "outcome"),
    [(False, "completed"), (False, "failed"), (True, "completed"), (True, "failed"), (True, "cancelled")],
)
async def test_a_child_that_ran_and_did_not_complete_answers_its_parent_s_call_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    background: bool,
    outcome: Literal["completed", "failed", "cancelled"],
) -> None:
    """A child that failed or was cancelled answers its parent's call as failed, which Claude and Gemini read as one.

    The parent's model reads the same text either way; a child that completed answers as an ordinary result. The reply
    records a background child's call as the model read it, failed when the child did not complete.
    """
    child_model = ScriptedModel([answer("Child finished")])
    blocking = _BlockingChild()
    recorder = SpanRecorder(stopped=False)

    async def run_child(child: DelegationChild, **kwargs: Any) -> str:  # noqa: ANN401
        if outcome == "failed":
            msg = "provider down"
            raise RuntimeError(msg)
        if outcome == "cancelled":
            return await blocking(child)
        return await ai.run_delegated_child_response(child, **kwargs)

    if background:
        runtime = await tool_job_runtime(tmp_path)

        async def cancel_child() -> None:
            running = await blocking.running()
            await cancel_settled(runtime, subagent_job_id(runtime, running), owner=_BACKGROUND_PARENT, depth=0)

        response = await _drive_background_child(
            tmp_path,
            monkeypatch,
            runtime,
            run_child,
            while_waiting=cancel_child if outcome == "cancelled" else None,
            child_model=child_model,
            recorder=recorder,
        )
    else:
        response = await _drive_inline_child(tmp_path, monkeypatch, run_child, child_model=child_model)
    answered = answer_of(response, "waiting")
    expected = {
        "completed": "Child finished",
        "failed": "Delegation to 'code' failed: provider down",
        "cancelled": "Delegation cancelled.",
    }[outcome]
    assert str(answered.content).startswith(expected)
    assert bool(answered.tool_call_error) is (outcome != "completed")
    if background:
        content = str(answered.content)
        returned = content if outcome == "completed" else ToolOutcome(content=content, is_error=True)
        assert recorder.records[-1] == (
            "finished",
            "run_subagent",
            {"agent_name": "code", "task": "Research"},
            returned,
        )


async def _drive_inline_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_child: Callable[..., Awaitable[str]],
    *,
    child_model: ScriptedModel | None = None,
) -> RunRecord:
    """Drive one parent delegation whose child runs inline in the parent's call, without the job runtime."""
    paths = _runtime_paths(tmp_path)
    config = delegating_config(jobs=False)
    agents = ScriptedAgents({} if child_model is None else {"code": child_model})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, _BACKGROUND_PARENT, agents)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_BACKGROUND_PARENT)):
        paused = await leader.turn(
            calls(tool_call("run_subagent", "waiting", agent_name="code", task="Research")),
            answer("Parent done"),
        )
        return await leader.drive(paused, run_child)


@pytest.mark.asyncio
async def test_a_cancelled_subagent_job_ends_its_calls_after_hook_with_the_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job cancelled while its child runs still closes the call's after hook, with the cancellation as its error."""
    after_errors: list[BaseException | None] = []
    original_after = delegation_execution.after_delegation

    async def after(*args: object, error: BaseException | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        after_errors.append(error)
        await original_after(*args, error=error, **kwargs)

    monkeypatch.setattr(delegation_execution, "after_delegation", after)
    runtime = await tool_job_runtime(tmp_path)
    child = _BlockingChild()

    async def cancel() -> None:
        running = await child.running()
        await cancel_settled(runtime, subagent_job_id(runtime, running), owner=_BACKGROUND_PARENT, depth=0)

    await _drive_background_child(tmp_path, monkeypatch, runtime, child, while_waiting=cancel)
    assert [type(error) for error in after_errors] == [asyncio.CancelledError]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_a_subagent_calls_plugin_hooks_run_in_its_job_around_its_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocked: bool,
) -> None:
    """The before hook runs before the job starts the child, and the after hook gets the child's own result.

    A before hook that blocks the call fails its job without starting the child.
    """
    hooks: list[tuple[str, object]] = []
    original_before = delegation_execution.before_delegation
    original_after = delegation_execution.after_delegation

    async def before(**kwargs: Any) -> DelegationHookState:  # noqa: ANN401
        state = await original_before(**kwargs)
        hooks.append(("before", job_owns_execution()))
        if blocked:
            state.blocked_result = "Blocked by policy"
        return state

    async def after(*args: object, result: str | None, **kwargs: Any) -> None:  # noqa: ANN401
        hooks.append(("after", result))
        await original_after(*args, result=result, **kwargs)

    ran: list[DelegationChild] = []

    async def run_child(child: DelegationChild, **kwargs: Any) -> str:  # noqa: ANN401
        ran.append(child)
        return await ai.run_delegated_child_response(child, **kwargs)

    monkeypatch.setattr(delegation_execution, "before_delegation", before)
    monkeypatch.setattr(delegation_execution, "after_delegation", after)
    response = await _drive_background_child(
        tmp_path,
        monkeypatch,
        await tool_job_runtime(tmp_path),
        run_child,
        child_model=ScriptedModel([answer("Child finished")]),
    )
    answered = answer_of(response, "waiting")
    if blocked:
        assert (answered.content, answered.tool_call_error) == ("Blocked by policy", True)
        assert ran == []
        assert hooks == [("before", True), ("after", None)]
    else:
        assert str(answered.content).startswith("Child finished")
        assert len(ran) == 1
        assert hooks == [("before", True), ("after", answered.content)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_error", "cleanup_text"),
    [(OSError("native cleanup unavailable"), "native cleanup unavailable"), (OSError(), "OSError")],
    ids=["with_text", "without_text"],
)
async def test_child_failure_remains_primary_when_native_interruption_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: OSError,
    cleanup_text: str,
) -> None:
    """Failed child cleanup reports uncertainty without replacing or falsely settling the child.

    A cleanup error without text reports its type.
    """
    paths = _runtime_paths(tmp_path)
    config, owner, child = _prepared_child(paths)

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "primary execution failed"
        raise RuntimeError(msg)

    async def interrupt(*_args: object, **_kwargs: object) -> None:
        raise cleanup_error

    finish = AsyncMock(side_effect=AssertionError("Unsettled native cleanup cannot be reported as terminal"))
    monkeypatch.setattr(delegation_execution, "interrupt_child", interrupt)
    monkeypatch.setattr(delegation_execution, "finish_child_turn", finish)
    runtime = await tool_job_runtime(tmp_path)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            await _start_job_body(runtime, child, owner=owner, config=config, paths=paths, run_child=run_child)
            outcome = await _ended(runtime, child, owner)
    finally:
        await runtime.shutdown()

    assert outcome.status == "failed"
    assert "primary execution failed" in (outcome.result or "")
    assert (outcome.result or "").endswith(
        f"Delegation cleanup did not complete; recovery may still be required: {cleanup_text}",
    )
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
    """Receipt failure cannot erase the execution error or overwrite the child's terminal evidence."""
    paths = _runtime_paths(tmp_path)
    config, owner, child = _prepared_child(paths)

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "primary execution failed"
        raise RuntimeError(msg)

    async def fail_receipt(*_args: object, **_kwargs: object) -> str:
        msg = "terminal receipt unavailable"
        raise OSError(msg)

    monkeypatch.setattr(delegation_execution, "finish_child_turn", fail_receipt)
    runtime = await tool_job_runtime(tmp_path)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            await _start_job_body(runtime, child, owner=owner, config=config, paths=paths, run_child=run_child)
            outcome = await _ended(runtime, child, owner)
    finally:
        await runtime.shutdown()

    assert outcome.status == "failed"
    assert "primary execution failed" in (outcome.result or "")
    assert "settlement" in (outcome.result or "").lower()
    assert "terminal receipt unavailable" in (outcome.result or "")
    retained = await load_retained_subagent_turn(child, paths)
    assert retained.status == "failed"
    assert retained.result == "primary execution failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "agent_mode", "excluded", "offered"),
    [
        (0, "standard", False, True),
        (1, "standard", False, False),
        (0, "minimal", False, False),
        (0, "standard", True, False),
    ],
)
async def test_only_a_build_whose_subagent_calls_become_jobs_offers_background_waits(
    tmp_path: Path,
    depth: int,
    agent_mode: AgentMode,
    excluded: bool,
    offered: bool,
) -> None:
    """Only calls that can become jobs are offered a background wait.

    A nested subagent's calls run inside its parent's job, a minimal agent's return to the CLI command that made them,
    and an excluded toolkit's keep their native wait.
    """
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            "code": AgentConfig(display_name="Code"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        models={"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        background_tool_jobs={"enabled": True, "exclude_toolkits": ["delegate"] if excluded else []},
    )
    persist_entity_accounts(config, paths)
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    try:
        build = create_agent(
            "leader",
            config,
            paths,
            owner,
            knowledge=None,
            model=turn_model(config, paths, "leader"),
            plugins=config_plugins(config, paths),
            delegation_depth=depth,
            agent_mode=agent_mode,
        )
        # A minimal agent's CLI describes each function with its toolkit's guidance.
        delegate = next(toolkit for toolkit in build.spec.toolkits if toolkit.name == "delegate")
        [run_subagent] = (function for function in delegate.functions if function.name == "run_subagent")
        instructed = config.get_prompt("DELEGATE_BACKGROUND_JOB_INSTRUCTIONS") in (delegate.guidance or "")
        assert ("background_after" in (run_subagent.description or ""), instructed) == (offered, offered)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_childs_outcome_counts_as_read_only_once_its_parent_run_saves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's outcome counts as read once the parent run that waited for it is saved with its answer."""
    await _drive_background_child(
        tmp_path,
        monkeypatch,
        await tool_job_runtime(tmp_path),
        ai.run_delegated_child_response,
        child_model=ScriptedModel([answer("Child answer")]),
    )
    [job] = (await saved_jobs(tmp_path)).values()
    assert job.status == "completed"
    assert job.consumed


@pytest.mark.asyncio
async def test_a_follow_up_waits_until_the_earlier_turns_job_has_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child's turn settles before its job finishes delivering its answer; a follow-up until then is refused as busy.

    So the handle names the earlier turn until its job ended, and that job's recovery always finds the turn it ran.
    """
    finishing, release = asyncio.Event(), asyncio.Event()
    original_after = delegation_execution.after_delegation

    async def slow_after(*args: object, **kwargs: Any) -> None:  # noqa: ANN401
        finishing.set()
        await release.wait()
        await original_after(*args, **kwargs)

    monkeypatch.setattr(delegation_execution, "after_delegation", slow_after)
    paths = _runtime_paths(tmp_path)
    config = delegating_config()
    identity = _identity()
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    agents = ScriptedAgents({"code": ScriptedModel([answer("First answer"), answer("Follow-up answer")])})
    agents.install(monkeypatch)
    leader = DelegatingLeader(config, paths, identity, agents)

    async def follow_up(subagent_id: str, run_id: str) -> tuple[str, bool]:
        record = await asyncio.wait_for(
            leader.reply(
                calls(tool_call("continue_subagent", "again", subagent_id=subagent_id, message="And now?")),
                answer("Done"),
                run_id=run_id,
            ),
            JOB_TEST_TIMEOUT,
        )
        answered = answer_of(record, "again")
        return str(answered.content), bool(answered.tool_call_error)

    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            first = await leader.reply(
                calls(tool_call("run_subagent", "first", task="Report", agent_name="code", background_after=0)),
                answer("Parent free"),
            )
            handle = json.loads(str(answer_of(first, "first").content))
            await asyncio.wait_for(finishing.wait(), JOB_TEST_TIMEOUT)
            assert (await load_retained_subagent_turn(delegation_child(runtime.retained_jobs()[0]), paths)).status == (
                "completed"
            )
            assert await follow_up(handle["subagent_id"], "early") == (SUBAGENT_BUSY, True)
            release.set()
            await wait_for_status(runtime, handle["job_id"], "completed")
            monkeypatch.setattr(delegation_execution, "after_delegation", original_after)
            answered, failed = await follow_up(handle["subagent_id"], "later")
            assert answered.startswith("Follow-up answer")
            assert not failed
    finally:
        release.set()
        await runtime.shutdown()
