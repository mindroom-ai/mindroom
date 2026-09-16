"""Native background jobs retain exact execution and approval ownership."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import background as background_module
from mindroom.delegation.background import BackgroundSubagentRuntime, register_background_runtime
from mindroom.delegation.control import (
    HumanMessageSignal,
    SubagentControl,
    human_message_signal_context,
    subagent_control_context,
)
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.model_control import install_subagent_model_control
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.state import DelegationState
from mindroom.response_turn import ResponsePausedForApproval, paused_attempt_from_response
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import (
    DelegationModel,
    _call,
    _saved_approval_calls,
)
from tests.test_delegation_execution import (
    test_child_approval_survives_parent_reconstruction as _native_approval_scenario,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild


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
    runtime = BackgroundSubagentRuntime(tmp_path)
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

    def blocked_writer(path: Path, payload: object) -> None:
        original_writer(path, payload)
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
            assert DelegationState.from_metadata(response.metadata).children == []
            assert children[0].status == "running"
    finally:
        release_writer.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)
        storage.close()


def test_native_wait_is_external_execution() -> None:
    """Wait must transfer exact child approvals through the native driver."""

    async def wait_subagent(job_id: str) -> str:
        return job_id

    toolkit = Toolkit(name="delegate", tools=[wait_subagent])
    apply_tool_approval_capability(
        toolkit,
        Config(),
        supports_native_tool_approval=True,
        registered_tool_name="delegate",
    )
    assert toolkit.async_functions["wait_subagent"].external_execution is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("detach", "human"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(("approval", "cancel_approval"), [(False, False), (True, False), (True, True)])
async def test_native_background_result_runs_child_once(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detach: bool,
    approval: bool,
    human: bool,
    cancel_approval: bool,
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
    runtime = BackgroundSubagentRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    monkeypatch.setattr("mindroom.delegation.execution._FOREGROUND_WAIT_SECONDS", 0.01 if detach and not human else 10)
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
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
        return Agent(
            name="leader",
            db=storage,
            tools=[toolkit],
            model=DelegationModel(
                id="test",
                responses=[ModelResponse(tool_calls=[call]), ModelResponse(content="Parent free")],
            ),
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

    try:
        with (
            tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)),
            human_message_signal_context(signal),
        ):
            current_parent = parent(_call("run_subagent", "first", task="Report", agent_name="code"))
            result = await asyncio.wait_for(drive(current_parent), 5)
            assert result.status == (RunStatus.paused if approval and not detach else RunStatus.completed)
            assert len(children) == 1
            child = children[0]
            if not approval or detach:
                first = next(message.content for message in result.messages if message.tool_call_id == "first")
                assert f"Subagent ID: {child.subagent_id}" in first
            if detach:
                assert f"Job ID: {child.delegation_id}" in first
                assert not completed.is_set()
                assert DelegationState.from_metadata(result.metadata).children == []
                if human:
                    assert "Status: paused_for_human" in await toolkit.inspect_subagent(child.delegation_id)
                    assert "Status: running" in await toolkit.resume_subagent(child.delegation_id)
                release.set()
                await asyncio.wait_for(completed.wait(), 5)
                monkeypatch.setattr("mindroom.delegation.execution._FOREGROUND_WAIT_SECONDS", 10)
                current_parent = parent(_call("wait_subagent", "wait", job_id=child.delegation_id))
                result = await drive(current_parent)
                if not approval:
                    message = next(message.content for message in result.messages if message.tool_call_id == "wait")
                    assert "Exact child result" in message
                    assert child.subagent_id in message
                assert len(children) == 1
            elif not approval:
                assert "Exact child result" in first
            if approval:
                assert result.status == RunStatus.paused
                assert side_effects == []
                state = DelegationState.from_metadata(result.metadata)
                call = _saved_approval_calls(state)[0]
                assert call.toolkit_name == "file"
                assert call.invoking_agent == "code"
                assert call.tool_call_id == f"{child.delegation_id}:write-once"
                if cancel_approval:
                    assert "Status: cancelled" in await toolkit.cancel_subagent(child.delegation_id)
                    cancelled = await read_child_run(child, config, paths)
                    assert cancelled is not None
                    assert cancelled.status == RunStatus.cancelled
                    assert side_effects == []
                    return
                rebuilt = Agent(
                    name="leader",
                    db=storage,
                    tools=[toolkit],
                    model=DelegationModel(id="test", responses=[ModelResponse(content="Approved parent result")]),
                )
                persisted = await rebuilt.aget_run_output(result.run_id, session_id="parent")
                result = await drive_delegations(
                    rebuilt,
                    persisted,
                    run_child=run_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                    decisions={call.tool_call_id: True},
                    denial_reasons={call.tool_call_id: None},
                    approval_calls=(call,),
                )
                assert result.status == RunStatus.completed
                assert side_effects == ["written"]
                assert len(children) == 1
                assert any("Exact child result" in (message.content or "") for message in result.messages)
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)
        storage.close()
        for child_storage in child_storages:
            child_storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_human_pause_stops_next_provider_invocation_without_cancelling_active_request(stream: bool) -> None:
    """Provider-native tools cannot start in a fresh request while the owning job is paused."""
    entered = asyncio.Event()
    release = asyncio.Event()
    called = asyncio.Event()

    class ProviderModel(DelegationModel):
        async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
            entered.set()
            await release.wait()
            return ModelResponse(content="Provider finished")

        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            entered.set()
            await release.wait()
            yield ModelResponse(content="Provider finished")

    model = ProviderModel(id="test")
    install_subagent_model_control(model, None)
    control = SubagentControl()

    async def invoke() -> str:
        called.set()
        if stream:
            chunks = [chunk async for chunk in model.ainvoke_stream()]
            return str(chunks[0].content)
        return str((await model.ainvoke()).content)

    with subagent_control_context(control):
        first = asyncio.create_task(invoke())
        await entered.wait()
        control.pause()
        release.set()
        assert await asyncio.wait_for(first, 1) == "Provider finished"
        entered.clear()
        called.clear()
        second = asyncio.create_task(invoke())
        await called.wait()
        assert not entered.is_set()
        assert not second.done()
        control.resume()
        assert await asyncio.wait_for(second, 1) == "Provider finished"


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
    runtime = BackgroundSubagentRuntime(tmp_path)
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
        jobs = await runtime.recover()
        assert len(jobs) == 1
        assert jobs[0].status == ("completed" if outcome in {"approve", "cancel_completed"} else "cancelled")
        if outcome != "approve":
            assert jobs[0].child.status == jobs[0].status
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)
