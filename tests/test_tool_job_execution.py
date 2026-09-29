"""Real SDK calls retain one execution after the parent releases its wait."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from dataclasses import replace
from typing import TYPE_CHECKING, Never

import pytest
from agno.agent import Agent
from agno.exceptions import AgentRunException, StopAgentRun
from agno.media import Image
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunContentEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import FunctionCall, ToolResult
from pydantic import BaseModel

from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.hooks import HookRegistry
from mindroom.tool_jobs import agno_execution, results
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.consumption import (
    ConsumptionOwner,
    consume_tool_job,
    consumption_context,
    set_consumption_storage,
)
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import (
    current_execution_resources,
    defer_execution_cleanup,
    execution_resources,
)
from mindroom.tool_jobs.results import (
    ToolResultPayload,
    _decode_result_payload,
    encode_result_payload,
    encode_tool_result,
    read_result_payload,
)
from mindroom.tool_jobs.runtime import read_job_snapshot, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import assembled_function, tool_job_runtime, wait_for_status

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
    """State, stream replay, and control data are encoded with the tool value off the event loop."""
    text = "payload" * 16_384
    loop_thread = threading.get_ident()
    encoding_threads: list[int] = []

    def encode(payload: ToolResultPayload) -> dict[str, object]:
        encoding_threads.append(threading.get_ident())
        return encode_result_payload(payload)

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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(streamed if kind == "stream" else tool)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="payload-run", session_id=context.session_id, session_state={})
    monkeypatch.setattr(agno_execution, "encode_result_payload", encode)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                result = await model.arun_function_call(FunctionCall(function=function, call_id="payload-call"))
        assert len(encoding_threads) == 1
        assert encoding_threads[0] != loop_thread
        owner = build_execution_identity_from_runtime_context(context)
        listed = (await runtime.list_jobs(owner=owner, depth=0))[0]
        payload = await read_result_payload(runtime, await runtime.lookup(listed.job_id, owner=owner, depth=0))
        if kind == "stream":
            assert payload.value == text * 2
        else:
            assert payload.state_delta["large"]["value"] == text
        if kind == "control":
            assert payload.control == {
                "message": "stop requested",
                "user_message": None,
                "agent_message": text,
                "messages": None,
                "stop_execution": True,
            }
            assert isinstance(result[0], AgentRunException)
            assert result[0].agent_message == text
        else:
            assert result[0] is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancellation_during_encoding_drains_resources_and_keeps_returned_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: bool,
) -> None:
    """An operation that returned before cancellation still owns its completed encoded outcome."""
    started, release, closed = threading.Event(), threading.Event(), asyncio.Event()
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
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

    def encode(payload: ToolResultPayload) -> dict[str, object]:
        started.set()
        assert release.wait(5)
        return encode_result_payload(payload)

    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(tool)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="encoding", session_id=context.session_id, session_state={})
    monkeypatch.setattr(agno_execution, "encode_result_payload", encode)
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
                    stopping = asyncio.create_task(runtime.cancel(job_id, owner=owner, depth=0))
                    await wait_for_status(runtime, job_id, "cancel_requested")
                assert not closed.is_set()
                release.set()
                await stopping
                assert closed.is_set()
                saved = read_job_snapshot(tmp_path / "tool_jobs" / f"{job_id}.json")
                assert (saved.status, saved.result) == ("completed", "completed before cancellation")
                # Shutdown has closed the runtime, so decode the saved payload file itself.
                payload_file = tmp_path / "tool_jobs" / f"{job_id}.g0.result.json"
                assert _decode_result_payload(json.loads(payload_file.read_text())).state_delta == {
                    "changed": {"before_present": False, "before": None, "present": True, "value": "encoded state"},
                }
    finally:
        release.set()
        await runtime.shutdown()


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
    runtime = tool_job_runtime(tmp_path)
    if managed:
        pin_background_tool_jobs(context.config, paths)
        register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "coding",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    prepend_tool_hook_bridge(toolkit, build_tool_hook_bridge(HookRegistry.empty(), agent_name="leader"))
    bind_toolkit_authority(toolkit, authored_name="coding")
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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "exact-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(slow_tool)])
    try:
        async with execution_resources():
            with tool_runtime_context(context), human_message_signal_context(signal):
                parent = asyncio.create_task(agent.arun("start", session_id=context.session_id))
                await asyncio.wait_for(started.wait(), 30)
                signal.notify()
                done, _ = await asyncio.wait({parent}, timeout=30)
                try:
                    assert parent in done, "managed ordinary tool must release its parent on human follow-up"
                    response = parent.result()
                    assert response.tools is not None
                    handle = response.tools[0].result
                    job_id = json.loads(handle)["job_id"]
                    signal.clear()
                    release.set()
                    completed = await asyncio.wait_for(
                        runtime.wait(job_id, owner=build_execution_identity_from_runtime_context(context), depth=0),
                        30,
                    )
                    assert completed.job.status == "completed"
                    assert completed.job.result == "actual result"
                    await runtime.release_wait(job_id, completed.claim)
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
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_streamed_result_saves_its_text_and_media_once(tmp_path: Path) -> None:
    """The saved job keeps full text and media only in its payload value; metadata keeps a bounded summary."""
    text = "".join(f"chunk {index:04d} " for index in range(200))

    async def streamed() -> AsyncIterator[RunContentEvent | ToolResult | str]:
        yield RunContentEvent(content=text[:1000])
        yield ToolResult(content=text[1000:1500], images=[Image(content=b"image")])
        yield text[1500:]

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(streamed)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="once-run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                _, _, call, result = await model.arun_function_call(FunctionCall(function=function, call_id="once"))
        replayed = list(call.result)
        assert isinstance(replayed[0], RunContentEvent)
        assert replayed[0].content == text[:1000]
        assert replayed[1:] == [text[1000:1500], text[1500:]]
        assert result.result.content == text
        assert result.images[0].content == b"image"
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.result == text[:500]
        assert job.summary_truncated
        files = sorted((tmp_path / "tool_jobs").glob(f"{job.job_id}.*"))
        assert [path.name for path in files] == [f"{job.job_id}.g0.result.json", f"{job.job_id}.json"]
        saved = "".join(path.read_text() for path in files)
        for marker in ("chunk 0060 ", "chunk 0100 ", "chunk 0180 "):
            assert saved.count(marker) == 1
        assert saved.count(base64.b64encode(b"image").decode()) == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_oversized_result_becomes_a_failed_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result beyond the encoded limit fails its job with the size-limit error instead of being saved."""

    async def large() -> str:
        return "x" * 200

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(large)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="large-run", session_id=context.session_id, session_state={})
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 150)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                success, _, call, _ = await model.arun_function_call(FunctionCall(function=function, call_id="large"))
        assert success is False
        assert "encoded JSON limit" in call.error
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.status == "failed"
        assert not job.has_result_payload
    finally:
        await runtime.shutdown()


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
async def test_run_connected_toolkit_call_stays_inline_through_human_followup(
    tmp_path: Path,
    team_parent: bool,
) -> None:
    """A human follow-up cannot detach a call whose SDK connection lasts only for its run."""
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
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    toolkit = ConnectionTools()
    bind_toolkit_authority(toolkit, authored_name="connection")
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
            release.set()
            response = await asyncio.wait_for(parent, 2)
        assert response.tools is not None
        assert response.tools[0].result == "connected result"
        assert toolkit.closes == 1
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        release.set()
        await runtime.shutdown()


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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("generated", "generator-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(generated)])

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
                payload = await read_result_payload(runtime, result.job)
                assert isinstance(payload.value, ToolResult)
                assert payload.value.content == "event textpicture"
                assert payload.value.images[0].content == b"image"
                assert payload.value.metadata == {"detail": "retained"}
                assert [length for length, _event in payload.replay] == [len("event text"), len("picture")]
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_exact_call_reattachment_rejects_changed_arguments(tmp_path: Path) -> None:
    """One SDK identity cannot launch a second invocation with changed arguments."""
    invocations = []

    async def record(value: str) -> str:
        invocations.append(value)
        return value

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(record)
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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
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
    function = assembled_function(fast_tool)
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
        assert jobs[0].adapter["arguments"] == encode_tool_result({"wait_timeout": None})
        assert jobs[0].source_event_id == "$original-request"
        assert jobs[0].owner == owner
        assert jobs[0].consumed is not save_fails
        assert len(await runtime.pending_outcomes()) == int(save_fails)
        await runtime.shutdown()
        restored = tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            saved = await restored.lookup(jobs[0].job_id, owner=owner, depth=0)
            assert saved.source_event_id == "$original-request"
            assert saved.owner == owner
            assert saved.consumed is not save_fails
            assert len(await restored.pending_outcomes()) == int(save_fails)
            assert saved.adapter["arguments"] == encode_tool_result({"wait_timeout": None})
        finally:
            await restored.shutdown()
    finally:
        storage.close()
        await runtime.shutdown()


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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
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
    bind_toolkit_authority(toolkit, authored_name="slow")
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
            cancelling = asyncio.create_task(runtime.cancel(job_id, owner=owner, depth=0))
            await wait_for_status(runtime, job_id, "cancel_requested")
            assert not finished.is_set()
            done, _ = await asyncio.wait({cancelling}, timeout=0.05)
            assert not done, "cancellation settled while synchronous side effects were still running"
            release.set()
            result = await cancelling
            assert finished.is_set()
            assert result.status == "cancelled"
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("team_event", [False, True])
async def test_fast_generator_preserves_sdk_events(tmp_path: Path, *, structured: bool, team_event: bool) -> None:
    """A drained fast generator still forwards its supported SDK events once."""

    class Answer(BaseModel):
        value: str

    event_type = TeamRunContentEvent if team_event else RunContentEvent
    content = Answer(value="saved answer") if structured else "event text"
    expected = '{"value":"saved answer"}' if structured else "event text"

    async def generated() -> AsyncIterator[RunContentEvent | TeamRunContentEvent | str]:
        yield event_type(content=content)
        yield "tail"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("generated", "event-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(generated)])
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
        tool_message = next(message for message in model.seen_messages if message.role == "tool")
        assert not tool_message.tool_call_error
        assert tool_message.content == expected + "tail"
        assert sum(isinstance(event, event_type) and event.content == expected for event in events) == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "control"])
async def test_cancellation_receipt_does_not_replay_the_original_outcome(tmp_path: Path, outcome: str) -> None:
    """A saved cancel result acknowledges control without applying old errors or state changes."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
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
    function = assembled_function(original)
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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
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
            Agent(
                id="leader",
                model=model,
                tools=[assembled_function(change_state)],
                db=storage,
                session_state={"counter": 0},
            ),
        )
        assert storage.get_run(first.run_id).session_state["counter"] == 1
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.consumed
        if restart:
            await runtime.shutdown()
            runtime = tool_job_runtime(tmp_path)
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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(change_state)
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
                    value, _ = await consume_tool_job(runtime, waited.job, waited.claim, function_call=call)
                    await claims.finalize()
                assert state["changed"] == 1
                assert state["conflict"] == state["unrelated"] == "newer"
                assert value.metadata["session_state_conflicts"] == ["conflict"]
    finally:
        release.set()
        await runtime.shutdown()


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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    state = {"changed": 0, "conflict": "old"}
    function = assembled_function(generated)
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
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(cached)
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


@pytest.mark.asyncio
async def test_saved_control_exception_keeps_stop_semantics(tmp_path: Path) -> None:
    """Both original execution and later consumption preserve SDK stop control."""

    async def stop() -> str:
        message = "stop requested"
        raise StopAgentRun(message, agent_message="tool stopped")

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(stop)
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
                with pytest.raises(AgentRunException, match="stop requested") as stopped:
                    await JobTools(paths, owner).job(action="wait", job_id=jobs[0].job_id, wait_timeout=0)
                assert stopped.value.stop_execution
    finally:
        await runtime.shutdown()
