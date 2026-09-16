"""Real SDK calls retain one execution after the parent releases its wait."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from dataclasses import replace
from typing import TYPE_CHECKING, Never

import anyio
import pytest
from agno.agent import Agent
from agno.exceptions import AgentRunException, StopAgentRun
from agno.media import Image
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunContentEvent
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall, ToolResult

from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import (
    ConsumptionOwner,
    consume_tool_job,
    consumption_context,
    set_consumption_storage,
)
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.resources import (
    connect_async_execution_resource,
    defer_execution_cleanup,
    disconnect_async_execution_resource,
    execution_resources,
)
from mindroom.tool_jobs.results import decode_tool_result, encode_tool_result
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput


@pytest.mark.asyncio
async def test_human_followup_releases_original_sdk_call_once(tmp_path: Path) -> None:
    """A follow-up releases the parent while its original tool keeps running."""
    started, release = asyncio.Event(), asyncio.Event()
    invocations = 0

    async def slow_tool() -> str:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return "actual result"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "exact-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[slow_tool])
    try:
        async with execution_resources():
            with tool_runtime_context(context), human_message_signal_context(signal):
                parent = asyncio.create_task(agent.arun("start", session_id=context.session_id))
                await asyncio.wait_for(started.wait(), 2)
                signal.notify()
                done, _ = await asyncio.wait({parent}, timeout=0.5)
                try:
                    assert parent in done, "managed ordinary tool must release its parent on human follow-up"
                    response = parent.result()
                    assert response.tools is not None
                    handle = response.tools[0].result
                    job_id = json.loads(handle)["job_id"]
                    release.set()
                    jobs = await runtime.list_jobs(
                        owner=build_execution_identity_from_runtime_context(context),
                        depth=0,
                    )
                    assert [job.job_id for job in jobs] == [job_id]
                    assert invocations == 1
                    assert response.tools[0].result == handle
                finally:
                    release.set()
                    await parent
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


def test_rich_result_codec_preserves_binary_media() -> None:
    """Durable output retains typed binary artifacts and structured metadata."""
    expected = ToolResult(
        content="picture",
        images=[Image(content=b"\x00\xff")],
        metadata={"structured_content": {"a": 1}},
    )
    payload = json.loads(json.dumps(encode_tool_result(expected)))
    actual = decode_tool_result(payload)
    assert isinstance(actual, ToolResult)
    assert actual == expected


@pytest.mark.asyncio
async def test_resource_owner_releases_only_after_last_child() -> None:
    """Parent cleanup remains deferred until both accepted operations settle."""
    closed = []

    async def cleanup() -> None:
        closed.append("closed")

    async with execution_resources() as resources:
        first = resources.acquire()
        second = resources.acquire()
        assert defer_execution_cleanup(cleanup)
    assert closed == []
    await first.release()
    assert closed == []
    await second.release()
    assert closed == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("team_parent", [False, True])
async def test_sdk_toolkit_stays_connected_after_parent_handle(tmp_path: Path, team_parent: bool) -> None:
    """SDK teardown cannot close a toolkit still owned by its running job."""
    started, release = asyncio.Event(), asyncio.Event()

    class ConnectionTools(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            self.open = False
            self.closes = 0
            super().__init__(name="connection", tools=[self.read])

        def connect(self) -> None:
            self.open = True

        def close(self) -> None:
            self.open = False
            self.closes += 1

        async def read(self) -> str:
            started.set()
            await release.wait()
            assert self.open
            return "connected result"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    toolkit = ConnectionTools()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("read", "read-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[toolkit])
    if team_parent:
        agent = Team(
            id="leader",
            model=model,
            tools=[toolkit],
            members=[Agent(id="unused", model=DelegationModel(id="test"))],
        )

    @owned_tool_execution
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), 2)
            signal.notify()
            response = await asyncio.wait_for(parent, 2)
            assert toolkit.open, "SDK teardown closed an accepted tool's live connection"
            assert toolkit.closes == 0
            release.set()
            job_id = json.loads(response.tools[0].result)["job_id"]
            owner = build_execution_identity_from_runtime_context(context)
            signal.clear()
            result = await runtime.wait(job_id, owner=owner, depth=0)
            assert result.job.result == "connected result"
            assert toolkit.closes == 1
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_generator_result_finishes_inside_owned_operation(tmp_path: Path, fails: bool) -> None:
    """Generator output and failures settle before their execution resources close."""
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generated() -> AsyncIterator[RunContentEvent | ToolResult | str]:
        try:
            yield RunContentEvent(content="event text")
            started.set()
            await release.wait()
            if fails:
                msg = "iteration failed"
                raise ValueError(msg)
            yield ToolResult(content="picture", images=[Image(content=b"image")], metadata={"detail": "retained"})
        finally:
            closed.set()

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("generated", "generator-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[generated])

    @owned_tool_execution
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), 2)
            signal.notify()
            response = await asyncio.wait_for(parent, 2)
            assert not closed.is_set()
            job_id = json.loads(response.tools[0].result)["job_id"]
            release.set()
            signal.clear()
            result = await runtime.wait(job_id, owner=owner, depth=0)
            assert closed.is_set()
            assert result.job.status == ("failed" if fails else "completed")
            if not fails:
                value = decode_tool_result(result.job.result_payload["value"])
                assert isinstance(value, ToolResult)
                assert value.content == "event textpicture"
                assert value.images[0].content == b"image"
                assert decode_tool_result(result.job.result_payload["events"])[0]["content"] == "event text"
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
async def test_exact_call_reattachment_rejects_changed_arguments(tmp_path: Path) -> None:
    """One SDK identity cannot launch a second invocation with changed arguments."""
    invocations = []

    async def record(value: str) -> str:
        invocations.append(value)
        return value

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(record)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="same-run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                first = await model.arun_function_call(
                    FunctionCall(function=function, call_id="same-call", arguments={"value": "first"}),
                )
                again = await model.arun_function_call(
                    FunctionCall(function=function, call_id="same-call", arguments={"value": "first"}),
                )
                assert first[3].result == again[3].result == "first"
                with pytest.raises(ValueError, match="not available"):
                    await model.arun_function_call(
                        FunctionCall(function=function, call_id="same-call", arguments={"value": "changed"}),
                    )
        assert invocations == ["first"]
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("team_parent", [False, True])
@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize("approval", [False, True])
async def test_fast_result_acknowledges_exact_saved_sdk_run(  # noqa: PLR0915 - SDK, approval, receipt, and restart boundaries.
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    team_parent: bool,
    save_fails: bool,
    approval: bool,
) -> None:
    """A fast foreground result suppresses delivery only after database readback."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), membership_turn_id="$original-request")
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def fast_tool() -> str:
        return "actual result"

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("fast_tool", "exact-call", wait_timeout=None)]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    storage = storage_factory()
    function = Function.from_callable(fast_tool)
    function.requires_confirmation = approval
    agent = Agent(id="leader", model=model, tools=[function], db=storage)
    if team_parent:
        leader = DelegationModel(
            id="test",
            responses=[
                ModelResponse(
                    tool_calls=[_call("delegate_task_to_member", "member", member_id="leader", task="run tool")],
                ),
                ModelResponse(content="team done"),
            ],
        )
        install_tool_job_execution(leader)
        agent = Team(id="squad", model=leader, members=[agent], db=storage)
    if save_fails:

        def fail_save(*_args: object, **_kwargs: object) -> Never:
            msg = "storage unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(type(storage), "upsert_run", fail_save)

    @owned_tool_execution
    async def parent_run() -> RunOutput | TeamRunOutput:
        set_consumption_storage(storage_factory)
        response = await agent.arun("start", session_id=context.session_id, user_id=context.requester_id)
        if approval:
            assert await runtime.list_jobs(owner=owner, depth=0) == []
            for requirement in response.requirements or []:
                assert requirement.tool_execution.tool_args == {"wait_timeout": None}
                requirement.confirm()
            response = await agent.acontinue_run(run_response=response)
        return response

    try:
        with tool_runtime_context(context):
            response = await parent_run()
        if not team_parent:
            assert response.tools[0].result == "actual result"
        jobs = await runtime.list_jobs(owner=owner, depth=0)
        assert len(jobs) == 1
        assert decode_tool_result(jobs[0].adapter["arguments"]) == {"wait_timeout": None}
        assert jobs[0].adapter["source_event_id"] == "$original-request"
        assert jobs[0].owner == owner
        assert jobs[0].wait_acknowledged is not save_fails
        assert len(await runtime.pending_outcomes()) == int(save_fails)
        await runtime.shutdown()
        restored = ToolJobRuntime(tmp_path)
        try:
            await restored.recover()
            saved = await restored.lookup(jobs[0].job_id, owner=owner, depth=0)
            assert saved.adapter["source_event_id"] == "$original-request"
            assert saved.owner == owner
            assert decode_tool_result(saved.adapter["arguments"]) == {"wait_timeout": None}
        finally:
            await restored.shutdown()
    finally:
        storage.close()
        await runtime.shutdown()
        register_background_runtime(paths, None)


def test_rich_result_captures_local_artifact_before_cleanup(tmp_path: Path) -> None:
    """Durable media bytes survive deletion of a toolkit-owned file."""
    path = tmp_path / "image.png"
    path.write_bytes(b"artifact bytes")
    payload = encode_tool_result(ToolResult(content="image", images=[Image(filepath=path)]))
    path.unlink()
    result = decode_tool_result(payload)
    assert result.images[0].content == b"artifact bytes"
    assert result.images[0].filepath is None


@pytest.mark.asyncio
async def test_cancel_sync_job_waits_for_actual_thread(tmp_path: Path) -> None:
    """Cancellation cannot release resources before the actual sync worker exits."""
    started, release = threading.Event(), threading.Event()
    finished = threading.Event()

    def slow_tool() -> str:
        started.set()
        release.wait(5)
        finished.set()
        return "finished"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "sync-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[slow_tool])

    @owned_tool_execution
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            parent = asyncio.create_task(parent_run())
            assert await asyncio.to_thread(started.wait, 2)
            signal.notify()
            response = await asyncio.wait_for(parent, 2)
            job_id = json.loads(response.tools[0].result)["job_id"]
            result = await runtime.cancel(job_id, owner=owner, depth=0)
            assert result.status == "cancel_requested"
            assert not finished.is_set()
            cancelling = asyncio.create_task(runtime.cancel(job_id, owner=owner, depth=0, await_completion=True))
            done, _ = await asyncio.wait({cancelling}, timeout=0.05)
            assert not done, "cancellation settled while synchronous side effects were still running"
            release.set()
            result = await cancelling
            assert finished.is_set()
            assert result.status in {"completed", "cancelled"}
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("fastmcp", [False, True])
async def test_sdk_mcp_connection_closes_in_original_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fastmcp: bool,
) -> None:
    """SDK MCP owner retains both task affinity and real FastMCP transport lifetime."""
    started, release = asyncio.Event(), asyncio.Event()
    owners = []
    sdk_mcp = pytest.importorskip("agno.tools.mcp.mcp", exc_type=ImportError)

    async def read() -> str:
        started.set()
        await release.wait()
        return "connected result"

    class AffineMCP(sdk_mcp.MCPTools):
        async def connect(self, force: bool = False) -> None:  # noqa: ARG002 - SDK connection signature.
            owners.append(asyncio.current_task())
            self._initialized = True
            self.register(read)

        async def close(self) -> None:
            assert asyncio.current_task() is owners[0]
            self._initialized = False

    if fastmcp:
        fastmcp_module = pytest.importorskip("fastmcp")
        server = fastmcp_module.FastMCP("test-server")
        server.tool(read)
        client = fastmcp_module.Client(server)
        monkeypatch.setattr(sdk_mcp, "_build_fastmcp_client", lambda *_args, **_kwargs: client)
        toolkit = sdk_mcp.MCPTools(command="unused")
    else:
        toolkit = AffineMCP(command="unused")
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("read", "mcp-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[toolkit])

    @owned_tool_execution
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), 3)
            signal.notify()
            response = await asyncio.wait_for(parent, 3)
            assert toolkit.initialized
            job_id = json.loads(response.tools[0].result)["job_id"]
            release.set()
            signal.clear()
            result = await runtime.wait(job_id, owner=owner, depth=0)
            assert result.job.status == "completed"
            assert "connected result" in result.job.result
            assert not toolkit.initialized
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
async def test_shared_task_affine_connection_waits_for_both_job_owners() -> None:
    """Shared async connections close once in the original AnyIO cancel-scope task."""
    resource = object()
    scope = anyio.CancelScope()
    connections = []
    closures = []

    async def connect() -> None:
        connections.append(asyncio.current_task())
        scope.__enter__()

    async def close() -> None:
        closures.append(asyncio.current_task())
        scope.__exit__(None, None, None)

    async with execution_resources() as first:
        await connect_async_execution_resource(resource, connect, close)
        first_job = first.acquire()
        await disconnect_async_execution_resource(resource)
    async with execution_resources() as second:
        await connect_async_execution_resource(resource, connect, close)
        second_job = second.acquire()
        await disconnect_async_execution_resource(resource)
    assert len(connections) == 1
    await first_job.release()
    assert closures == []
    await second_job.release()
    assert closures == connections


@pytest.mark.asyncio
async def test_fast_generator_preserves_sdk_events(tmp_path: Path) -> None:
    """A drained fast generator still forwards its supported SDK events once."""

    async def generated() -> AsyncIterator[RunContentEvent | ToolResult | str]:
        yield RunContentEvent(content="event text")
        yield "tail"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("generated", "event-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[generated])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                events = [
                    event
                    async for event in agent.arun(
                        "start",
                        session_id=context.session_id,
                        stream=True,
                        stream_events=True,
                    )
                ]
        assert sum(isinstance(event, RunContentEvent) and event.content == "event text" for event in events) == 1
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
async def test_later_consumption_merges_only_changed_state_and_reports_conflicts(tmp_path: Path) -> None:
    """Background mutation stays isolated, and later consumption preserves newer keys."""
    started, release = asyncio.Event(), asyncio.Event()

    async def change_state(run_context: RunContext) -> str:
        started.set()
        await release.wait()
        run_context.session_state.update({"changed": 1, "conflict": "tool"})
        return "result"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(change_state)
    function._agent = Agent(id="leader")
    state = {"changed": 0, "conflict": "old", "unrelated": "old"}
    function._run_context = RunContext(run_id="origin", session_id=context.session_id, session_state=state)
    call = FunctionCall(function=function, call_id="state-call")
    try:
        async with execution_resources():
            with tool_runtime_context(context), human_message_signal_context(signal):
                parent = asyncio.create_task(model.arun_function_call(call))
                await asyncio.wait_for(started.wait(), 2)
                signal.notify()
                returned = await parent
                job_id = json.loads(returned[3].result)["job_id"]
                release.set()
                signal.clear()
                waited = await runtime.wait(job_id, owner=owner, depth=0)
                assert state == {"changed": 0, "conflict": "old", "unrelated": "old"}
                state.update({"conflict": "newer", "unrelated": "newer"})
                claims = ConsumptionOwner()
                with consumption_context(claims):
                    value = await consume_tool_job(runtime, waited.job, waited.token, function_call=call)
                    await claims.finalize()
                assert state["changed"] == 1
                assert state["conflict"] == state["unrelated"] == "newer"
                assert value.metadata["session_state_conflicts"] == ["conflict"]
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
async def test_sdk_cache_and_post_hook_never_receive_job_handles(tmp_path: Path) -> None:
    """The copied actual call alone fills raw-result cache and post hooks."""
    calls, observed = [], []

    async def cached() -> str:
        calls.append("invoked")
        return "actual result"

    def after(fc: FunctionCall) -> None:
        observed.append(fc.result)

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(cached)
    function.cache_results = True
    function.cache_dir = str(tmp_path / "cache")
    function.post_hook = after
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="cache-run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                for index in range(2):
                    result = await model.arun_function_call(FunctionCall(function=function, call_id=str(index)))
                    assert result[3].result == "actual result"
        assert calls == ["invoked"]
        assert observed == ["actual result", "actual result"]
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
async def test_saved_control_exception_keeps_stop_semantics(tmp_path: Path) -> None:
    """Both original execution and later consumption preserve SDK stop control."""

    async def stop() -> str:
        message = "stop requested"
        raise StopAgentRun(message, agent_message="tool stopped")

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(stop)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="control-run", session_id=context.session_id, session_state={})
    call = FunctionCall(function=function, call_id="control-call")
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                result = await model.arun_function_call(call)
                assert isinstance(result[0], AgentRunException)
                assert result[0].stop_execution
                jobs = await runtime.list_jobs(owner=owner, depth=0)
                waited = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
                claims = ConsumptionOwner()
                with consumption_context(claims):
                    with pytest.raises(AgentRunException, match="stop requested") as stopped:
                        await consume_tool_job(runtime, waited.job, waited.token, function_call=call)
                    assert stopped.value.stop_execution
                    await claims.finalize()
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)
