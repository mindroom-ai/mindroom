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
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.hooks import HookRegistry
from mindroom.tool_jobs import agno_execution
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
    current_execution_resources,
    defer_execution_cleanup,
    disconnect_async_execution_resource,
    execution_resources,
)
from mindroom.tool_jobs.results import decode_tool_result, encode_tool_result
from mindroom.tool_jobs.runtime import ToolJobRuntime, read_job_snapshot, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["state", "stream", "control"])
async def test_large_outcome_encoding_leaves_event_loop_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """State, events, replay, and control data use the same worker as the tool value."""
    text = "payload" * 16_384
    loop_thread = threading.get_ident()
    encoding_threads: list[int] = []

    def encode(value: object) -> object:
        if text in str(value):
            encoding_threads.append(threading.get_ident())
        return encode_tool_result(value)

    async def tool(run_context: RunContext) -> str:
        run_context.session_state["large"] = text
        if kind == "control":
            message = "stop requested"
            raise StopAgentRun(message, agent_message=text)
        return "saved"

    async def streamed() -> AsyncIterator[RunContentEvent | str]:
        yield RunContentEvent(content=text)
        yield text

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(streamed if kind == "stream" else tool)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="payload-run", session_id=context.session_id, session_state={})
    monkeypatch.setattr(agno_execution, "encode_tool_result", encode)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                result = await model.arun_function_call(FunctionCall(function=function, call_id="payload-call"))
        assert encoding_threads
        assert all(thread != loop_thread for thread in encoding_threads)
        owner = build_execution_identity_from_runtime_context(context)
        listed = (await runtime.list_jobs(owner=owner, depth=0))[0]
        job = await runtime.lookup(listed.job_id, owner=owner, depth=0)
        if kind == "stream":
            assert decode_tool_result(job.result_payload["events"])[0]["content"] == text
            assert decode_tool_result(job.result_payload["replay"])[-1]["text"] == text
        else:
            assert decode_tool_result(job.result_payload["state_delta"])["large"]["value"] == text
        if kind == "control":
            assert isinstance(result[0], AgentRunException)
            assert result[0].agent_message == text
        else:
            assert result[0] is True
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancellation_during_encoding_drains_resources_and_keeps_returned_value(  # noqa: PLR0915 - SDK, resources, cancellation, and durable output.
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: bool,
) -> None:
    """An operation that returned before cancellation still owns its completed encoded outcome."""
    started, release, closed = threading.Event(), threading.Event(), asyncio.Event()
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    async def cleanup() -> None:
        closed.set()

    async def tool(run_context: RunContext) -> str:
        resources = current_execution_resources()
        assert resources is not None
        reference = resources.acquire()
        assert defer_execution_cleanup(cleanup)
        await reference.release()
        run_context.session_state["changed"] = "encoded state"
        return "completed before cancellation"

    def encode(value: object) -> object:
        if isinstance(value, dict) and "changed" in value:
            started.set()
            assert release.wait(5)
        return encode_tool_result(value)

    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(tool)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="encoding", session_id=context.session_id, session_state={})
    monkeypatch.setattr(agno_execution, "encode_tool_result", encode)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                result = await model.arun_function_call(
                    FunctionCall(function=function, call_id="call", arguments={"wait_timeout": 0}),
                )
                job_id = json.loads(result[3].result)["job_id"]
                assert await asyncio.to_thread(started.wait, 5)
                if shutdown:
                    stopping = asyncio.create_task(runtime.shutdown())
                    await asyncio.sleep(0)
                else:
                    requested = await runtime.cancel(job_id, owner=owner, depth=0)
                    assert requested.status == "cancel_requested"
                    stopping = asyncio.create_task(runtime.cancel(job_id, owner=owner, depth=0, await_completion=True))
                assert not closed.is_set()
                release.set()
                await stopping
                assert closed.is_set()
                saved = read_job_snapshot(tmp_path / "tool_jobs" / f"{job_id}.json")
                assert saved.status == "completed"
                assert saved.result == "completed before cancellation"
                assert decode_tool_result(saved.result_payload["state_delta"])["changed"]["value"] == "encoded state"
    finally:
        release.set()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("managed", "wait_timeout"), [(False, None), (True, None), (True, 0)])
async def test_registered_sync_tool_completes_through_sdk_dispatch(
    tmp_path: Path,
    managed: bool,
    wait_timeout: float | None,
) -> None:
    """The registered sync hook bridge must not give its job a foreign-loop completion task."""
    (tmp_path / "input.txt").write_text("registered sync result\n")
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=["coding"])},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=managed),
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    if managed:
        register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "coding",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    prepend_tool_hook_bridge(toolkit, build_tool_hook_bridge(HookRegistry.empty(), agent_name="leader"))
    arguments: dict[str, object] = {"path": "input.txt"}
    if managed:
        arguments["wait_timeout"] = wait_timeout
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("read_file", "sync-call", **arguments)]),
            ModelResponse(content="done"),
        ],
    )
    if managed:
        install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[toolkit])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await agent.arun("read the input", session_id=context.session_id)
                assert response.tools is not None
                tool = response.tools[0]
                if managed:
                    jobs = await runtime.list_jobs(owner=owner, depth=0)
                    assert len(jobs) == 1
                    waited = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
                    assert waited.job.status == "completed", waited.job.result
                    assert "registered sync result" in waited.job.result
                if wait_timeout is None:
                    assert not tool.tool_call_error, tool.result
                    assert "registered sync result" in tool.result
                else:
                    assert json.loads(tool.result)["job_id"] == jobs[0].job_id
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


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
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_sdk_toolkit_stays_connected_after_parent_handle(
    tmp_path: Path,
    team_parent: bool,
    cleanup_fails: bool,
) -> None:
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
        response = await agent.arun("start", session_id=context.session_id)
        if cleanup_fails:

            async def close_storage() -> None:
                message = "storage close failed after execution"
                raise OSError(message)

            assert defer_execution_cleanup(close_storage)
        return response

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
            assert result.job.status == "completed"
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
        await runtime.quiesce()
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
            assert saved.wait_acknowledged is not save_fails
            assert len(await restored.pending_outcomes()) == int(save_fails)
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
@pytest.mark.parametrize("with_bridge", [False, True])
async def test_cancel_sync_job_waits_for_actual_thread(tmp_path: Path, with_bridge: bool) -> None:
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
    toolkit = Toolkit(name="slow", tools=[slow_tool])
    if with_bridge:
        prepend_tool_hook_bridge(toolkit, build_tool_hook_bridge(HookRegistry.empty(), agent_name="leader"))
    agent = Agent(id="leader", model=model, tools=[toolkit])

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
@pytest.mark.parametrize("outcome", ["completed", "failed", "control"])
async def test_cancellation_receipt_does_not_replay_the_original_outcome(tmp_path: Path, outcome: str) -> None:
    """A saved cancel result acknowledges control without applying old errors or state changes."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    async def original(run_context: RunContext) -> str:
        run_context.session_state["counter"] = 1
        if outcome == "failed":
            message = "original execution failed"
            raise RuntimeError(message)
        if outcome == "control":
            message = "original execution stopped"
            raise StopAgentRun(message)
        return "original output"

    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(original)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(
        run_id="original-run",
        session_id=context.session_id,
        session_state={"counter": 0},
    )

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    storage = storage_factory()
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                await model.arun_function_call(FunctionCall(function=function, call_id="original-call"))
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        model.responses = [
            ModelResponse(tool_calls=[_call("job", "cancel-call", action="cancel", job_id=job.job_id)]),
            ModelResponse(content="Cancelled."),
        ]
        actor = Agent(
            id="leader",
            model=model,
            tools=[JobTools(paths, owner)],
            db=storage,
            session_state={"counter": 0},
        )

        @owned_tool_execution
        async def cancel() -> RunOutput:
            set_consumption_storage(storage_factory)
            with tool_runtime_context(context):
                return await actor.arun("cancel", session_id=context.session_id, user_id=context.requester_id)

        response = await cancel()
        assert not response.tools[0].tool_call_error
        assert json.loads(response.tools[0].result)["job_id"] == job.job_id
        saved = storage.get_run(response.run_id)
        assert saved.session_state["counter"] == 0
        assert await runtime.pending_outcomes() == []
    finally:
        storage.close()
        await runtime.shutdown()
        register_background_runtime(paths, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("later_counter", [0, 1, 9])
async def test_saved_result_reread_preserves_later_session_state(
    tmp_path: Path,
    restart: bool,
    later_counter: int,
) -> None:
    """Old output is readable across turns and restart without repeating mutations or conflict warnings."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def change_state(run_context: RunContext) -> str:
        run_context.session_state["counter"] = 1
        return "original output"

    @owned_tool_execution
    async def run(agent: Agent) -> RunOutput:
        set_consumption_storage(storage_factory)
        with tool_runtime_context(context):
            return await agent.arun(
                "continue",
                session_id=context.session_id,
                user_id=context.requester_id,
                session_state=agent.session_state,
            )

    storage = storage_factory()
    try:
        model = DelegationModel(
            id="test",
            responses=[ModelResponse(tool_calls=[_call("change_state", "change")]), ModelResponse(content="done")],
        )
        install_tool_job_execution(model)
        first = await run(
            Agent(id="leader", model=model, tools=[change_state], db=storage, session_state={"counter": 0}),
        )
        assert storage.get_run(first.run_id).session_state["counter"] == 1
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.wait_acknowledged
        if restart:
            await runtime.shutdown()
            runtime = ToolJobRuntime(tmp_path)
            await runtime.recover()
            register_background_runtime(paths, runtime)
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "reread", action="wait", job_id=job.job_id)]),
                ModelResponse(content="read again"),
            ],
        )
        install_tool_job_execution(model)
        later = await run(
            Agent(
                id="leader",
                model=model,
                tools=[JobTools(paths, owner)],
                db=storage,
                session_state={"counter": later_counter},
                overwrite_db_session_state=True,
            ),
        )
        saved = storage.get_run(later.run_id)
        assert saved.session_state["counter"] == later_counter
        assert saved.tools[0].result == "original output"
        assert await runtime.pending_outcomes() == []
    finally:
        storage.close()
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
@pytest.mark.parametrize("rich", [False, True])
async def test_streamed_state_conflict_notice_reaches_sdk_tool_message(tmp_path: Path, *, rich: bool) -> None:
    """Replayed generator output retains consumption warnings and newer parent state."""
    started, release = asyncio.Event(), asyncio.Event()

    async def generated(run_context: RunContext) -> AsyncIterator[str | ToolResult | RunContentEvent]:
        started.set()
        await release.wait()
        run_context.session_state.update({"changed": 1, "conflict": "tool"})
        yield RunContentEvent(content="progress")
        yield ToolResult(content="Result.", images=[Image(content=b"image")]) if rich else "Result."

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    state = {"changed": 0, "conflict": "old"}
    function = Function.from_callable(generated)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state=state)
    call = FunctionCall(function=function, call_id="stream-state", arguments={})
    messages = []

    @owned_tool_execution
    async def dispatch() -> list[object]:
        with tool_runtime_context(context):
            return [event async for event in model.arun_function_calls([call], messages)]

    pending = asyncio.create_task(dispatch())
    try:
        await asyncio.wait_for(started.wait(), 2)
        state["conflict"] = "newer"
        release.set()
        events = await pending
        assert state["changed"] == 1
        assert state["conflict"] == "newer"
        assert len(messages) == 1
        assert "Result." in messages[0].content
        assert messages[0].content.count("Session state conflicts: conflict") == 1
        assert sum(isinstance(event, RunContentEvent) and event.content == "progress" for event in events) == 1
        if rich:
            assert messages[0].images[0].content == b"image"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
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
