"""Native background jobs retain exact execution and approval ownership."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.models.fallback import FallbackConfig
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput, ToolCallCompletedEvent
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.custom_tools.job import JobTools
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation.background import continue_delegation, delegation_child
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.lifecycle import prepare_child_turn, start_child_turn
from mindroom.delegation.recovery import RESTART_INTERRUPTION_REASON, read_child_run
from mindroom.delegation.sessions import load_retained_subagent_turn, subagent_recovery_lock
from mindroom.delegation.state import DelegationState
from mindroom.response_turn import ResponsePausedForApproval, paused_attempt_from_response
from mindroom.tool_jobs import runtime as background_module
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.control import (
    HumanMessageSignal,
    JobControl,
    human_message_signal_context,
    job_control_context,
    job_owns_execution,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    read_job_snapshot,
    register_background_runtime,
    saved_job_paths,
)
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    DelegationModel,
    _call,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
)
from tests.test_delegation_execution import (
    test_child_approval_survives_parent_reconstruction as _native_approval_scenario,
)
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    job_child,
    start_delegation_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_jobs.runtime import ToolJobRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize(
    ("tool_name", "budget", "depth", "excluded"),
    [
        ("run_subagent", "bad", 0, False),
        ("run_subagent", 0, 1, False),
        ("continue_subagent", -1, 0, False),
        ("job", True, 0, False),
        ("run_subagent", 0, 0, True),
        ("run_subagent", None, 0, True),
        ("continue_subagent", 0, 0, True),
    ],
)
async def test_invalid_native_wait_resolves_exact_requirement_without_child_execution(  # noqa: PLR0915
    tmp_path: Path,
    persisted: bool,
    tool_name: str,
    budget: object,
    depth: int,
    excluded: bool,
) -> None:
    """Fresh and restored external requirements return correctable tool failures before admission."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            "code": AgentConfig(display_name="Code"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    if excluded:
        config.background_tool_jobs.exclude_toolkits.append("delegate")
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    delegate = DelegateTools("leader", ["code"], paths, config, execution_identity=owner)
    bind_toolkit_construction(delegate, ToolConstruction("delegate", None))
    bind_toolkit_authority(delegate, authored_name="delegate")
    apply_tool_approval_capability(
        delegate,
        config,
        supports_native_tool_approval=True,
        registered_tool_name="delegate",
    )
    jobs = JobTools(paths, owner)
    arguments = {"task": "must-not-run", "agent_name": "code", "wait_timeout": budget}
    if tool_name == "continue_subagent":
        arguments = {"subagent_id": "missing", "message": "must-not-run", "wait_timeout": budget}
    elif tool_name == "job":

        async def saved_outcome() -> BackgroundOutcome:
            return BackgroundOutcome("completed", "retained")

        child = replace(
            delegation_child(completed_delegation_job()),
            caller_agent_name="leader",
            child_agent_name="code",
        )
        retained = await start_delegation_job(runtime, child, owner=owner, operation=saved_outcome)
        waited = await runtime.wait(retained.job_id, owner=owner, depth=0)
        await runtime.release_wait(retained.job_id, waited.claim)
        arguments = {"action": "wait", "job_id": retained.job_id, "wait_timeout": budget}
        if persisted:
            jobs.async_functions["job"].external_execution = True
            jobs.async_functions["job"].external_execution_silent = True
            jobs.async_functions["job"].approval_type = "mindroom_job_wait"
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call(tool_name, "invalid", **arguments)]),
            ModelResponse(content="corrected"),
        ],
    )
    if not persisted:
        install_tool_job_execution(model, depth=depth)
    storage = create_session_storage("leader", config, paths, owner)
    parent = Agent(id="leader", model=model, db=storage, tools=[delegate, jobs])

    async def run_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "Invalid wait metadata must not execute a child"
        raise RuntimeError(msg)

    try:
        async with execution_resources():
            with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
                paused = await parent.arun("delegate", session_id=owner.session_id, user_id=owner.requester_id)
                assert paused.status == RunStatus.paused
                if persisted:
                    paused = RunOutput.from_dict(paused.to_dict())
                    install_tool_job_execution(model, depth=depth)
                response = await drive_delegations(
                    parent,
                    paused,
                    run_child=run_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=owner,
                    delegation_depth=depth,
                )
        assert response.status == RunStatus.completed
        assert response.content == "corrected"
        rejected = next(tool for tool in response.tools if tool.tool_call_id == "invalid")
        assert rejected.tool_call_error
        assert "wait_timeout" in rejected.result
        assert rejected.tool_args == arguments
        assert any(
            message.tool_call_id == "invalid" and "wait_timeout" in message.content for message in model.seen_messages
        )
        assert DelegationState.from_metadata(response.metadata).children == []
        assert len(await runtime.list_jobs(owner=owner, depth=0)) == (1 if tool_name == "job" else 0)
    finally:
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
async def test_parent_cancellation_during_job_admission_keeps_accepted_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before start returns must still relinquish the parent's child ownership."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=owner)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, owner)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="test",
            responses=[ModelResponse(tool_calls=[_call("run_subagent", "call", task="Research", agent_name="code")])],
        ),
    )
    written, executing = asyncio.Event(), asyncio.Event()
    release_writer = threading.Event()
    loop = asyncio.get_running_loop()
    original_writer = background_module.write_json_file_durable
    children: list[DelegationChild] = []

    def blocked_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        original_writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        loop.call_soon_threadsafe(written.set)
        release_writer.wait()

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        children.append(child)
        executing.set()
        await asyncio.Event().wait()
        raise AssertionError

    monkeypatch.setattr(background_module, "write_json_file_durable", blocked_writer)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            response = await parent.arun("Delegate", session_id="parent", user_id=owner.requester_id)
            driving = asyncio.create_task(
                drive_delegations(
                    parent,
                    response,
                    run_child=run_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=owner,
                ),
            )
            await written.wait()
            driving.cancel()
            release_writer.set()
            with pytest.raises(asyncio.CancelledError):
                await driving
            await executing.wait()
            state = DelegationState.from_metadata(response.metadata)
            assert state.children == []
            assert len(state.hooks) == 1
            assert next(iter(state.hooks.values())).after_called
            assert children[0].status == "running"
    finally:
        release_writer.set()
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detach", "human", "approval", "cancel_approval", "duplicate_approval", "exclude_after_acceptance"),
    [
        (detach, human, approval, cancel, duplicate, excluded)
        for detach, human in [(False, False), (True, False), (True, True)]
        for approval, cancel, duplicate in [
            (False, False, None),
            (True, False, None),
            (True, True, None),
            (True, False, "other_parent"),
            (True, False, "same_parent"),
            (True, False, "next_child"),
            (True, False, "revoked"),
            (True, False, "unavailable"),
            (True, False, "denied_retrieval"),
            (True, False, "budgeted"),
        ]
        for excluded in [False, True]
        # Both successive children must be managed and remain in their foreground wait; a revoked resume is
        # presented through a later job wait, which needs the detached child.
        if (duplicate != "next_child" or not (detach or human or excluded))
        and (
            duplicate not in {"revoked", "unavailable", "denied_retrieval", "budgeted"}
            or (detach and not human and not excluded)
        )
    ],
)
async def test_native_background_result_runs_child_once(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detach: bool,
    approval: bool,
    human: bool,
    cancel_approval: bool,
    duplicate_approval: str | None,
    exclude_after_acceptance: bool,
) -> None:
    """A released parent cannot cancel the child; later waits read its exact result."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=["file"]),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    job_access = {"allowed": True}
    runtime = tool_job_runtime(tmp_path, authorize=lambda _job: job_access["allowed"])
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    bind_toolkit_authority(toolkit, authored_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    release = asyncio.Event()
    completed = asyncio.Event()
    children = []
    signal = HumanMessageSignal()
    side_effects = []
    child_storages = []
    child_responses = ([ModelResponse(tool_calls=[_call("write_report", "write-once")])] if approval else []) + [
        ModelResponse(content="Exact child result"),
    ]
    if duplicate_approval == "next_child":
        child_responses.extend(
            [
                ModelResponse(tool_calls=[_call("write_report", "write-twice")]),
                ModelResponse(content="Second child result"),
            ],
        )
    elif duplicate_approval and duplicate_approval != "budgeted":
        child_responses.insert(1, ModelResponse(tool_calls=[_call("write_report", "write-twice")]))

    async def write_report() -> str:
        side_effects.append("written")
        return "Report written"

    def build_child(*args: object, **kwargs: object) -> Agent:
        child_identity = args[3]
        child_storage = kwargs.get("history_storage") or create_session_storage("code", config, paths, child_identity)
        child_storages.append(child_storage)
        function = Function.from_callable(write_report)
        function.requires_confirmation = True
        function.owning_toolkit = "file"
        return Agent(
            name="code",
            id="code",
            db=child_storage,
            tools=[function],
            model=DelegationModel(id="test", responses=child_responses),
        )

    monkeypatch.setattr("mindroom.agents.create_agent", build_child)

    async def run_child(
        child: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        **_kwargs: object,
    ) -> str:
        children.append(child)
        if human:
            signal.notify()
        if detach:
            await release.wait()
        child_identity = replace(identity, agent_name="code", session_id=child.session_id)
        agent = build_child("code", config, runtime_paths, child_identity)
        try:
            response = await agent.arun(
                prompt,
                session_id=child.session_id,
                run_id=child.run_id,
                user_id=identity.requester_id,
            )
            completed.set()
            paused = paused_attempt_from_response(
                response,
                fallback_session_id=child.session_id,
                fallback_run_id=child.run_id,
                toolkit_owners=toolkit_owners_for_agents([agent]),
            )
            if paused is not None:
                raise ResponsePausedForApproval(paused)
            return "Exact child result"
        finally:
            agent.db.close()

    def parent(call: dict[str, object]) -> Agent:
        model = DelegationModel(
            id="test",
            responses=[ModelResponse(tool_calls=[call]), ModelResponse(content="Parent free")],
        )
        install_tool_job_execution(model)
        return Agent(
            name="leader",
            db=storage,
            tools=[toolkit, JobTools(paths, identity)],
            model=model,
        )

    async def drive(agent: Agent) -> object:
        response = await agent.arun("Delegate", session_id="parent", user_id=identity.requester_id)
        return await drive_delegations(
            agent,
            response,
            run_child=run_child,
            agent_name="leader",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
        )

    async def finish_approval_sequence(result: RunOutput) -> RunOutput:  # noqa: C901, PLR0915 - One ordered approval story.
        nonlocal runtime
        assert result.status == RunStatus.paused
        assert side_effects == []
        state = DelegationState.from_metadata(result.metadata)
        call = _saved_approval_calls(state)[0]
        # A background child's approval can resume only through its job, even when a later turn shows it again.
        parent_pause = paused_attempt_from_response(
            result,
            fallback_session_id="parent",
            fallback_run_id=result.run_id,
            toolkit_owners={},
        )
        assert parent_pause is not None
        assert parent_pause.job_owned_child
        if exclude_after_acceptance and not cancel_approval:
            await runtime.shutdown()
            runtime = tool_job_runtime(tmp_path)
            await runtime.recover()
            register_background_runtime(paths, runtime)
        assert (call.toolkit_name, call.invoking_agent, call.tool_call_id) == (
            "file",
            "code",
            f"{child.delegation_id}:write-once",
        )
        if cancel_approval:
            assert '"status": "cancelled"' in str(
                await JobTools(paths, identity).job("cancel", child.delegation_id),
            )
            cancelled = await read_child_run(child, config, paths)
            assert cancelled is not None
            assert cancelled.status == RunStatus.cancelled
            assert side_effects == []

        async def approve(
            paused: RunOutput,
            *,
            start_another: bool = False,
            on_event: Callable[[object], None] | None = None,
        ) -> RunOutput:
            saved_call = _saved_approval_calls(DelegationState.from_metadata(paused.metadata))[0]
            responses = [ModelResponse(content="Approved parent result")]
            if start_another:
                responses.insert(
                    0,
                    ModelResponse(
                        tool_calls=[
                            _call("run_subagent", "second-child", agent_name="code", task="Second report"),
                        ],
                    ),
                )
            rebuilt = Agent(
                name="leader",
                db=storage,
                tools=[toolkit, JobTools(paths, identity)],
                model=DelegationModel(id="test", responses=responses),
            )
            return await drive_delegations(
                rebuilt,
                deepcopy(paused),
                run_child=run_child,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                decisions={saved_call.tool_call_id: True},
                denial_reasons={saved_call.tool_call_id: None},
                approval_calls=(saved_call,),
                on_event=on_event,
            )

        if duplicate_approval == "budgeted":
            # An explicit budget still bounds the resumed wait; the approved tool it showed continues in the job.
            events: list[object] = []
            resumed = await approve(result, on_event=events.append)
            assert resumed.status == RunStatus.completed
            settled = [
                (bool(event.tool.tool_call_error), event.tool.result)
                for event in events
                if isinstance(event, ToolCallCompletedEvent) and event.tool.tool_name == "write_report"
            ]
            assert settled == [(False, f"Approved; background job {child.delegation_id} now owns it.")]
            await wait_for_status(runtime, child.delegation_id, "completed")
            assert side_effects == ["written"]
            return resumed
        if duplicate_approval in {"revoked", "unavailable"}:
            # Resuming a presented child approval rechecks current delegation policy and job access, like any
            # other resume, and settles the approved child tool its card showed.
            if duplicate_approval == "revoked":
                config.agents["leader"].delegate_to = []
            else:
                job_access["allowed"] = False
            events: list[object] = []
            revoked = await approve(result, on_event=events.append)
            assert revoked.status == RunStatus.completed
            assert side_effects == []
            settled = [
                event.tool
                for event in events
                if isinstance(event, ToolCallCompletedEvent) and event.tool.tool_name != "job"
            ]
            reason = "Cannot delegate" if duplicate_approval == "revoked" else "not available"
            assert [(tool.tool_call_error, reason in str(tool.result)) for tool in settled] == [(True, True)]
            job_access["allowed"] = True
            assert (await runtime.lookup(child.delegation_id, owner=identity, depth=0)).status == "awaiting_approval"
            return revoked
        if duplicate_approval:
            first_pause = deepcopy(result)
            if duplicate_approval == "other_parent":
                other_parent = parent(_call("job", "duplicate-wait", action="wait", job_id=child.delegation_id))
                result = await drive(other_parent)
                assert result.run_id != first_pause.run_id
            result = await approve(result, start_another=duplicate_approval == "next_child")
            assert result.status == RunStatus.paused
            assert side_effects == ["written"]
            pending_child = children[-1]
            before = await runtime.lookup(pending_child.delegation_id, owner=identity, depth=0)
            saved_parent = await current_parent.aget_run_output(result.run_id, session_id="parent")
            stale = await approve(first_pause)
            assert stale.status == (RunStatus.completed if duplicate_approval == "other_parent" else RunStatus.paused)
            assert await runtime.lookup(pending_child.delegation_id, owner=identity, depth=0) == before
            still_pending = await current_parent.aget_run_output(result.run_id, session_id="parent")
            assert still_pending.metadata == saved_parent.metadata
            assert still_pending.requirements == saved_parent.requirements
        events: list[object] = []
        result = await approve(result, on_event=events.append)
        assert result.status == RunStatus.completed
        assert side_effects == ([] if cancel_approval else ["written"] * (2 if duplicate_approval else 1))
        # The approved child tool its card showed is settled in the parent's live trace.
        settled = [
            (bool(event.tool.tool_call_error), event.tool.result)
            for event in events
            if isinstance(event, ToolCallCompletedEvent) and event.tool.tool_name == "write_report"
        ]
        assert settled == [
            (True, "Approval no longer applies: tool job is not awaiting this approval generation.")
            if cancel_approval
            else (False, "Report written"),
        ]
        assert len(children) == (2 if duplicate_approval == "next_child" else 1)
        for completed_child in children:
            saved_job = await runtime.lookup(completed_child.delegation_id, owner=identity, depth=0)
            assert saved_job.status == ("cancelled" if cancel_approval else "completed")
            assert saved_job.adapter["child"]["status"] == saved_job.status
        if not cancel_approval:
            assert any("Exact child result" in (message.content or "") for message in result.messages)
        return result

    try:
        with (
            tool_runtime_context(
                replace(
                    _delegate_runtime_context(config, paths, execution_identity=identity),
                    membership_turn_id="$native-reader",
                ),
            ),
            human_message_signal_context(signal),
        ):
            current_parent = parent(
                _call(
                    "run_subagent",
                    "first",
                    task="Report",
                    agent_name="code",
                    wait_timeout=0.01 if detach and not human else None,
                ),
            )
            result = await asyncio.wait_for(drive(current_parent), 30)
            assert result.status == (RunStatus.paused if approval and not detach else RunStatus.completed)
            assert len(children) == 1
            child = children[0]
            if exclude_after_acceptance:
                config.background_tool_jobs.exclude_toolkits.append("delegate")
            if not approval or detach:
                first = next(message.content for message in result.messages if message.tool_call_id == "first")
            if detach:
                handle = json.loads(first)
                assert set(handle) == {"job_id", "subagent_id", "status", "tool", "summary", "summary_truncated"}
                assert handle["job_id"] == child.delegation_id
                assert handle["subagent_id"] == child.subagent_id
                assert handle["tool"] == "delegate"
                assert handle["status"] == "running"
                assert not completed.is_set()
                assert DelegationState.from_metadata(result.metadata).children == []
                if human:
                    listed = json.loads(await JobTools(paths, identity).job("list"))
                    assert [item["status"] for item in listed if item["job_id"] == child.delegation_id] == ["running"]
                release.set()
                await asyncio.wait_for(completed.wait(), JOB_TEST_TIMEOUT)
                signal.clear()
                if duplicate_approval == "denied_retrieval":
                    # A denied approval for merely retrieving a job must leave its child untouched.
                    await wait_for_status(runtime, child.delegation_id, "awaiting_approval")
                    config.tool_approval.rules = [ApprovalRuleConfig(match="job", action="require_approval")]
                    gated = await drive(parent(_call("job", "gated-wait", action="wait", job_id=child.delegation_id)))
                    assert gated.status == RunStatus.paused
                    denied = await drive_delegations(
                        Agent(
                            name="leader",
                            db=storage,
                            tools=[toolkit, JobTools(paths, identity)],
                            model=DelegationModel(id="test", responses=[ModelResponse(content="Retrieval denied")]),
                        ),
                        gated,
                        run_child=run_child,
                        agent_name="leader",
                        config=config,
                        runtime_paths=paths,
                        execution_identity=identity,
                        decisions={"gated-wait": False},
                        denial_reasons={"gated-wait": None},
                    )
                    assert denied.status == RunStatus.completed
                    assert DelegationState.from_metadata(denied.metadata).children == []
                    job = await runtime.lookup(child.delegation_id, owner=identity, depth=0)
                    assert job.status == "awaiting_approval"
                    config.tool_approval.rules = []
                # Like the reply join, retrieve only once the job is ready: a result with no budget left to wait, and an
                # approval pause with no budget, so the approved call waits for the approved work like any other.
                await wait_for_status(runtime, child.delegation_id, "awaiting_approval" if approval else "completed")
                budget = {} if approval and duplicate_approval != "budgeted" else {"wait_timeout": 0}
                current_parent = parent(_call("job", "wait", action="wait", job_id=child.delegation_id, **budget))
                result = await drive(current_parent)
                if not approval:
                    message = next(message.content for message in result.messages if message.tool_call_id == "wait")
                    assert "Exact child result" in message
                    assert child.subagent_id in message
                assert len(children) == 1
            elif not approval:
                assert "Exact child result" in first
                assert child.subagent_id in first
            saved = await runtime.lookup(child.delegation_id, owner=identity, depth=0)
            assert (
                await runtime.outcome(
                    child.delegation_id,
                    saved.generation,
                    source_event_id="$native-reader",
                )
                is not None
            )
            if approval:
                result = await finish_approval_sequence(result)

    finally:
        release.set()
        await runtime.shutdown()
        storage.close()
        for child_storage in child_storages:
            child_storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_human_followup_does_not_stop_next_provider_invocation(
    fallback: bool,
    stream: bool,
) -> None:
    """Human follow-ups leave current and subsequent model requests running."""
    entered = asyncio.Event()
    release = asyncio.Event()
    called = asyncio.Event()
    closed = asyncio.Event()

    class ProviderModel(DelegationModel):
        async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
            entered.set()
            await release.wait()
            return ModelResponse(content="Provider finished")

        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            try:
                entered.set()
                await release.wait()
                yield ModelResponse(content="Provider finished")
                await asyncio.Event().wait()
            finally:
                closed.set()

    primary = ProviderModel(id="primary")
    fallback_model = ProviderModel(id="fallback")
    fallback_config = FallbackConfig(on_error=[fallback_model]) if fallback else None
    install_tool_job_execution(primary, fallback_config)
    model = fallback_model if fallback else primary
    signal = HumanMessageSignal()

    async def invoke() -> str:
        called.set()
        if stream:
            events = model.ainvoke_stream()
            try:
                return str((await anext(events)).content)
            finally:
                await events.aclose()
        return str((await model.ainvoke()).content)

    with human_message_signal_context(signal):
        first = asyncio.create_task(invoke())
        await entered.wait()
        signal.notify()
        release.set()
        assert await asyncio.wait_for(first, JOB_TEST_TIMEOUT) == "Provider finished"
        assert not stream or closed.is_set()
        entered.clear()
        called.clear()
        second = asyncio.create_task(invoke())
        await called.wait()
        assert entered.is_set()
        assert await asyncio.wait_for(second, JOB_TEST_TIMEOUT) == "Provider finished"


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("outcome", ["approve", "cancel", "cancel_removed", "cancel_completed"])
async def test_managed_team_approvals_keep_member_and_nested_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
    outcome: str,
) -> None:
    """Managed team jobs project the actual member and nested tool owner on reconstruction."""
    paths = _runtime_paths(tmp_path)
    runtime = tool_job_runtime(tmp_path)
    # The scenario authors the default background tool job setting.
    pin_background_tool_jobs(Config(), paths)
    register_background_runtime(paths, runtime)
    try:
        await _native_approval_scenario(
            tmp_path,
            monkeypatch,
            outcome=outcome,
            siblings=1,
            nested=nested,
            retry=False,
            team_parent=True,
        )
        [saved] = [read_job_snapshot(path) for path in saved_job_paths(tmp_path / "tool_jobs")]
        jobs = await runtime.list_jobs(owner=saved.owner, depth=saved.depth)
        assert len(jobs) == 1
        assert jobs[0].status == ("completed" if outcome in {"approve", "cancel_completed"} else "cancelled")
        if outcome != "approve":
            assert delegation_child(jobs[0]).status == jobs[0].status
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_native_wait_unavailable_job_returns_tool_error(tmp_path: Path) -> None:
    """The native driver resolves generic lookup rejection instead of aborting its parent run."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            "code": AgentConfig(display_name="Code"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    agent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit, JobTools(paths, identity)],
        model=DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "missing-call", action="wait", job_id="missing")]),
                ModelResponse(content="Job unavailable"),
            ],
        ),
    )

    async def unused_child(*_args: object, **_kwargs: object) -> str:
        msg = "An unavailable job must not start native execution"
        raise AssertionError(msg)

    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await agent.arun("Wait", session_id="parent", user_id=identity.requester_id)
            result = await drive_delegations(
                agent,
                response,
                run_child=unused_child,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
        assert result.status == RunStatus.completed
        content = next(message.content for message in result.messages if message.tool_call_id == "missing-call")
        assert "not available in this conversation" in content
    finally:
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
async def test_early_child_failure_retains_liveness_through_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery cannot claim a child between startup failure and durable terminal settlement."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    child = prepare_child_turn("leader", "code", "task", owner=owner, config=config, runtime_paths=paths, depth=0)
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
                decisions=None,
                denial_reasons=None,
                approval_calls=(),
                fresh=True,
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
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    storage = create_session_storage("leader", config, paths, owner)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=owner)
    bind_toolkit_authority(toolkit, authored_name="delegate")
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("run_subagent", "failing", agent_name="code", task="Research")]),
            ModelResponse(content="Parent done"),
        ],
    )
    parent = Agent(id="leader", name="leader", model=model, db=storage, tools=[toolkit], telemetry=False)
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
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
            paused = await parent.arun("Delegate", session_id=owner.session_id, user_id=owner.requester_id)
            pending = asyncio.create_task(
                drive_delegations(
                    parent,
                    paused,
                    run_child=run_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=owner,
                ),
            )
            try:
                await asyncio.wait_for(settling.wait(), JOB_TEST_TIMEOUT)
                with subagent_recovery_lock(children[0].subagent_id, paths) as acquired:
                    assert not acquired, "Failure settlement released its exact live child too early"
            finally:
                release.set()
            response = await pending
        assert response.status == RunStatus.completed
        with subagent_recovery_lock(children[0].subagent_id, paths) as acquired:
            assert acquired
    finally:
        storage.close()


async def _drive_background_child(
    tmp_path: Path,
    runtime: ToolJobRuntime,
    run_child: Callable[..., Awaitable[str]],
    *,
    while_waiting: Callable[[], Awaitable[None]] | None = None,
) -> RunOutput:
    """Drive one parent delegation whose child runs as a managed job, acting once the child starts."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    storage = create_session_storage("leader", config, paths, _BACKGROUND_PARENT)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=_BACKGROUND_PARENT)
    bind_toolkit_authority(toolkit, authored_name="delegate")
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("run_subagent", "waiting", agent_name="code", task="Research")]),
            ModelResponse(content="Parent done"),
        ],
    )
    parent = Agent(id="leader", name="leader", model=model, db=storage, tools=[toolkit], telemetry=False)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_BACKGROUND_PARENT)):
            paused = await parent.arun(
                "Delegate",
                session_id=_BACKGROUND_PARENT.session_id,
                user_id=_BACKGROUND_PARENT.requester_id,
            )
            pending = asyncio.create_task(
                drive_delegations(
                    parent,
                    paused,
                    run_child=run_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=_BACKGROUND_PARENT,
                ),
            )
            if while_waiting is not None:
                await while_waiting()
            return await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
    finally:
        await runtime.shutdown()
        storage.close()


_BACKGROUND_PARENT = ToolExecutionIdentity(
    "matrix",
    "leader",
    "@alice:example.org",
    "!room:example.org",
    None,
    None,
    "parent",
)


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
async def test_shutdown_interrupts_a_running_child_like_a_restart(tmp_path: Path, shutdown: bool) -> None:
    """Only a cancellation request cancels a running child; shutdown records the restart interruption."""
    runtime = tool_job_runtime(tmp_path)
    child = _BlockingChild()

    async def stop() -> None:
        running = await child.running()
        if shutdown:
            await runtime.quiesce()
        else:
            await runtime.cancel(running.delegation_id, owner=_BACKGROUND_PARENT, depth=0)

    await _drive_background_child(tmp_path, runtime, child, while_waiting=stop)
    (path,) = saved_job_paths(tmp_path / "tool_jobs")
    job = read_job_snapshot(path)
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
        await delegation_execution._interrupt_cancelled_child(
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

    with job_control_context(control), patch.object(delegation_execution, "interrupt_child", new=interrupt):
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
        (RESTART_INTERRUPTION_REASON, "failed")
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
async def test_revoked_child_wait_becomes_the_parent_call_result(tmp_path: Path) -> None:
    """Losing access while the parent waits on its background child ends the call, not the parent's reply."""
    allowed = True
    runtime = tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    child = _BlockingChild()

    async def revoke() -> None:
        nonlocal allowed
        await child.running()
        allowed = False
        await runtime.cancel_revoked(denied=lambda _job: True)

    response = await _drive_background_child(tmp_path, runtime, child, while_waiting=revoke)
    assert response.status == RunStatus.completed
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
    response = await _drive_background_child(tmp_path, tool_job_runtime(tmp_path), run_child)
    assert response.status == RunStatus.completed


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
        tool_job_runtime(tmp_path, authorize=lambda _job: False),
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
        await _drive_background_child(tmp_path, tool_job_runtime(tmp_path), run_child)
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
    child = prepare_child_turn("leader", "code", "task", owner=owner, config=config, runtime_paths=paths, depth=0)

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
            decisions=None,
            denial_reasons=None,
            approval_calls=(),
            fresh=True,
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
    child = prepare_child_turn("leader", "code", "task", owner=owner, config=config, runtime_paths=paths, depth=0)

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
            decisions=None,
            denial_reasons=None,
            approval_calls=(),
            fresh=True,
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
async def test_native_job_source_survives_approval_continuation_and_restart(
    tmp_path: Path,
    source_event_id: str | None,
) -> None:
    """Approval turns may update native child state without replacing its accepted human source."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader"), "code": AgentConfig(display_name="Code")})
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        membership_turn_id=source_event_id,
    )
    child = prepare_child_turn("leader", "code", "task", owner=owner, config=config, runtime_paths=paths, depth=0)
    runtime = tool_job_runtime(tmp_path)

    async def approval() -> BackgroundOutcome:
        child.status = "paused"
        return BackgroundOutcome("awaiting_approval")

    async def completed() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "finished once"
        return BackgroundOutcome("completed", child.result)

    try:
        with tool_runtime_context(context):
            job = await start_delegation_job(runtime, child, owner=owner, operation=approval)
        waited = await runtime.wait(job.job_id, owner=owner, depth=0)
        assert waited.job.source_event_id == source_event_id
        assert waited.job.owner == owner
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        with tool_runtime_context(replace(context, membership_turn_id="$approval-request")):
            await continue_delegation(
                runtime,
                job.job_id,
                owner=owner,
                depth=0,
                expected_generation=0,
                operation=completed,
            )
        waited = await runtime.wait(job.job_id, owner=owner, depth=0)
        assert waited.job.source_event_id == source_event_id
        assert waited.job.result == "finished once"
        await runtime.release_wait(job.job_id, waited.claim)
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        saved = await restored.lookup(child.delegation_id, owner=owner, depth=0)
        assert saved.source_event_id == source_event_id
        assert saved.owner == owner
        assert delegation_child(saved).run_id == child.run_id
        assert saved.status == "completed"
    finally:
        await restored.shutdown()
