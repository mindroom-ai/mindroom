"""Real SDK calls retain one execution after the parent releases its wait."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Never

import pytest
from agno.agent import Agent
from agno.exceptions import AgentRunException, StopAgentRun
from agno.media import Image
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunContentEvent
from agno.run.base import RunStatus
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import FunctionCall, ToolResult
from pydantic import BaseModel

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.hooks import HookRegistry
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs import agno_execution, results
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.consumption import (
    ConsumptionOwner,
    consume_tool_job,
    consumption_context,
    set_consumption_storage,
)
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.conversation import ConversationKey
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
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    SpanRecorder,
    assembled_function,
    cancel_settled,
    lookup,
    pending_outcomes,
    saved_jobs,
    saved_payload,
    tool_job_runtime,
    wait_for_status,
)

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
    runtime = await tool_job_runtime(tmp_path)
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
        payload = await read_result_payload(runtime, await lookup(runtime, listed.job_id, owner=owner, depth=0))
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
    runtime = await tool_job_runtime(tmp_path)
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
                    FunctionCall(function=function, call_id="call", arguments={"background_after": 0}),
                )
                job_id = json.loads(result[3].result)["job_id"]
                assert await asyncio.to_thread(started.wait, JOB_TEST_TIMEOUT)
                if shutdown:
                    stopping = asyncio.create_task(runtime.shutdown())
                    await asyncio.sleep(0)
                else:
                    stopping = asyncio.create_task(cancel_settled(runtime, job_id, owner=owner, depth=0))
                    await wait_for_status(runtime, job_id, "cancel_requested")
                assert not closed.is_set()
                release.set()
                await stopping
                assert closed.is_set()
                saved = (await saved_jobs(tmp_path))[job_id]
                assert (saved.status, saved.result) == ("completed", "completed before cancellation")
                # Shutdown has closed the runtime, so decode the saved payload itself.
                assert _decode_result_payload(await saved_payload(tmp_path, job_id)).state_delta == {
                    "changed": {"before_present": False, "before": None, "present": True, "value": "encoded state"},
                }
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("managed", "background_after"), [(False, None), (True, None), (True, 0)])
async def test_registered_sync_tool_completes_through_sdk_dispatch(
    tmp_path: Path,
    managed: bool,
    background_after: float | None,
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
    runtime = await tool_job_runtime(tmp_path)
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
        arguments["background_after"] = background_after
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
                if background_after is None:
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "exact-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(slow_tool)])
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                parent = asyncio.create_task(agent.arun("start", session_id=context.session_id))
                await asyncio.wait_for(started.wait(), 30)
                signal.set()
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
    runtime = await tool_job_runtime(tmp_path)
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
        payload = await saved_payload(tmp_path, job.job_id)
        assert payload is not None
        saved = json.dumps(asdict((await saved_jobs(tmp_path))[job.job_id])) + json.dumps(payload)
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
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
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

    @owned_tool_execution()
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            release.set()
            response = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("generated", "generator-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(generated)])

    @owned_tool_execution()
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            response = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
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
    runtime = await tool_job_runtime(tmp_path)
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
                # The refused call fails as a call; the reply's run goes on.
                changed = await model.arun_function_call(
                    FunctionCall(function=function, call_id="same-call", arguments={"value": "changed"}),
                )
                assert changed[0] is False
                assert "not available" in str(changed[2].error)
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def fast_tool() -> str:
        return "actual result"

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("fast_tool", "exact-call", background_after=None)]),
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

    @owned_tool_execution()
    async def parent_run() -> RunOutput | TeamRunOutput:
        set_consumption_storage(storage_factory)
        response = await agent.arun("start", session_id=context.session_id, user_id=context.requester_id)
        if approval:
            assert await runtime.list_jobs(owner=owner, depth=0) == []
            for requirement in response.requirements or []:
                assert requirement.tool_execution.tool_args == {"background_after": None}
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
        assert jobs[0].adapter["arguments"] == encode_tool_result({"background_after": None})
        assert jobs[0].source_event_id == "$original-request"
        assert jobs[0].owner == owner
        assert jobs[0].consumed is not save_fails
        assert len(pending_outcomes(runtime)) == int(save_fails)
        await runtime.shutdown()
        restored = await tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            saved = await lookup(restored, jobs[0].job_id, owner=owner, depth=0)
            assert saved.source_event_id == "$original-request"
            assert saved.owner == owner
            assert saved.consumed is not save_fails
            assert len(pending_outcomes(restored)) == int(save_fails)
            assert saved.adapter["arguments"] == encode_tool_result({"background_after": None})
        finally:
            await restored.shutdown()
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_bridge", [False, True])
async def test_cancel_returns_while_a_sync_job_waits_for_its_thread(tmp_path: Path, with_bridge: bool) -> None:
    """A cancel returns at once, and cannot settle or release resources before the actual sync worker exits."""
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
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

    @owned_tool_execution()
    async def parent_run() -> RunOutput | TeamRunOutput:
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            assert await asyncio.to_thread(started.wait, JOB_TEST_TIMEOUT)
            signal.set()
            response = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
            job_id = json.loads(response.tools[0].result)["job_id"]
            # The model's cancel returns at once; the job settles once the actual thread exits.
            requested = await asyncio.wait_for(runtime.cancel(job_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
            assert requested.status == "cancel_requested"
            assert not finished.is_set()
            drain = runtime._entries[job_id].drain
            assert drain is not None
            done, _ = await asyncio.wait({drain}, timeout=0.05)
            assert not done, "cancellation settled while synchronous side effects were still running"
            release.set()
            await wait_for_status(runtime, job_id, "cancelled")
            assert finished.is_set()
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
    runtime = await tool_job_runtime(tmp_path)
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
async def test_a_cancel_of_an_ended_job_does_not_replay_its_outcome(tmp_path: Path, outcome: str) -> None:
    """A cancel of an ended job neither replays its outcome's errors or state changes nor acknowledges it."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
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

        @owned_tool_execution()
        async def cancel() -> RunOutput:
            set_consumption_storage(storage_factory)
            with tool_runtime_context(context):
                return await actor.arun("cancel", session_id=context.session_id, user_id=context.requester_id)

        response = await cancel()
        assert not response.tools[0].tool_call_error
        assert json.loads(response.tools[0].result)["job_id"] == job.job_id
        saved = storage.get_run(response.run_id)
        assert saved.session_state["counter"] == 0
        # The job had already ended, so its outcome stays for the next notice or collect to report.
        assert [pending.job_id for pending in pending_outcomes(runtime)] == [job.job_id]
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def change_state(run_context: RunContext) -> str:
        run_context.session_state["counter"] = 1
        return "original output"

    @owned_tool_execution()
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
            runtime = await tool_job_runtime(tmp_path)
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
        assert pending_outcomes(runtime) == []
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = assembled_function(change_state)
    function._agent = Agent(id="leader")
    state = {"changed": 0, "conflict": "old", "unrelated": "old"}
    function._run_context = RunContext(run_id="origin", session_id=context.session_id, session_state=state)
    call = FunctionCall(function=function, call_id="state-call")
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                parent = asyncio.create_task(model.arun_function_call(call))
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                signal.set()
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
    runtime = await tool_job_runtime(tmp_path)
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

    @owned_tool_execution()
    async def dispatch() -> list[object]:
        with tool_runtime_context(context):
            return [event async for event in model.arun_function_calls([call], messages)]

    pending = asyncio.create_task(dispatch())
    try:
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
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
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["driver_type", "oversized", "missing_file", "cyclic"])
async def test_a_call_that_ran_never_reads_as_one_that_did_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    """A result the job cannot keep as returned still tells the model the call ran, so it is not repeated.

    A value with no wire tag keeps its text, as inline; a result over the size limit, whose media file is gone, or
    that refers to itself says the call took effect.
    """
    ran: list[str] = []

    async def create_event() -> dict[str, object] | ToolResult:
        ran.append("created")
        if result == "missing_file":
            return ToolResult(content="event card", images=[Image(filepath=tmp_path / "gone.png")])
        if result == "cyclic":
            cycle: dict[str, object] = {}
            cycle["self"] = cycle
            return cycle
        return {"at": datetime(2026, 10, 10, tzinfo=UTC)}

    if result == "oversized":
        monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 64)
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("create_event", "event-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(create_event)])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await agent.arun("Create", session_id=context.session_id)
        assert response.tools is not None
        [tool] = response.tools
        assert ran == ["created"]
        if result == "driver_type":
            assert not tool.tool_call_error
            assert "2026-10-10 00:00:00+00:00" in str(tool.result)
        else:
            assert tool.tool_call_error
            assert "The call ran" in str(tool.result)
            assert "do not call it again" in str(tool.result)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["waited", "released", "stopped"])
async def test_reply_records_a_managed_call_before_its_job_starts(tmp_path: Path, case: str) -> None:
    """A restart's account names a job's call as it names an inline one, and a Stop committed first refuses it."""
    ran: list[str] = []

    async def write_note(text: str) -> str:
        ran.append(text)
        return f"noted {text}"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    arguments: dict[str, object] = {"text": "hello"}
    if case == "released":
        arguments["background_after"] = 0
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("write_note", "note-call", **arguments)]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(write_note)])
    recorder = SpanRecorder(stopped=case == "stopped")
    owner = build_execution_identity_from_runtime_context(context)
    try:
        async with execution_resources():
            with tool_runtime_context(context), recording_tool_calls(recorder):
                if case == "stopped":
                    with pytest.raises(asyncio.CancelledError):
                        await agent.arun("Note", session_id=context.session_id)
                    assert (ran, recorder.records, await runtime.list_jobs(owner=owner, depth=0)) == ([], [], [])
                    return
                response = await agent.arun("Note", session_id=context.session_id)
        assert response.tools is not None
        result = response.tools[0].result
        # The record keeps the call's application arguments, never the reserved wait budget.
        assert recorder.records[0] == ("started", "write_note", {"text": "hello"}, None)
        assert recorder.records[1] == ("finished", "write_note", {"text": "hello"}, result)
        if case == "released":
            handle = json.loads(str(result))
            assert handle["tool"] == "write_note"
            await wait_for_status(runtime, handle["job_id"], "completed")
        else:
            assert result == "noted hello"
        assert ran == ["hello"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_whose_access_resolves_during_its_wait_returns_its_handle(tmp_path: Path) -> None:
    """A call that ran while its requester's access re-resolved returns its job handle, so the model never repeats it.

    The job stays held, so the reply waits for it and a wake delivers its result once the access resolves.
    """
    grant = {"access": "allowed"}
    ran: list[str] = []

    async def pay() -> str:
        ran.append("paid")
        # A membership refresh starts while the call runs.
        grant["access"] = "pending"
        return "paid"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(
        tmp_path,
        authorize=lambda _job: grant["access"] == "allowed",
        denied=lambda _job: False,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("pay", "pay-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(pay)])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await agent.arun("Pay", session_id=context.session_id)
        assert response.tools is not None
        [tool] = response.tools
        assert ran == ["paid"]
        assert not tool.tool_call_error
        handle = json.loads(str(tool.result))
        assert handle["tool"] == "pay"
        await wait_for_status(runtime, handle["job_id"], "completed")
        # Its outcome stays unconsumed for the wake.
        assert not runtime._entries[handle["job_id"]].job.consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["start", "authority"])
async def test_a_refused_job_fails_its_call_not_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """A call the job runtime refuses fails as that call; the run goes on.

    The runtime refuses the job while room membership re-resolves, or the call's authority before admission, as after
    a reload removed its toolkit while the model was answering.
    """
    ran: list[str] = []

    async def write_note() -> str:
        ran.append("note")
        return "noted"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: refusal != "start")
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    if refusal == "authority":

        def refuse(*_args: object) -> None:
            msg = "Tool job is not available in this conversation."
            raise JobAccessError(msg)

        monkeypatch.setattr(runtime, "authorize_execution", refuse)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("write_note", "note-call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(write_note)])
    recorder = SpanRecorder(stopped=False)

    async def cleanup() -> None:
        return None

    try:
        async with execution_resources():
            with tool_runtime_context(context), recording_tool_calls(recorder):
                response = await agent.arun("Note", session_id=context.session_id)
            # The refused call gave its resource reference back, so the reply's own cleanup is not deferred.
            assert defer_execution_cleanup(cleanup) is False
        assert response.status is RunStatus.completed
        assert response.content == "done"
        assert response.tools is not None
        assert response.tools[0].tool_call_error
        assert ran == []
        # A job the runtime refused finishes the record its call opened; a call refused before admission opens none.
        assert [record[0] for record in recorder.records] == (["started", "finished"] if refusal == "start" else [])
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_cancelled_while_its_reply_waits_tells_the_model_not_to_run_it_again(tmp_path: Path) -> None:
    """A person's cancel of a call the reply still waits on reaches the model as a reason, never an empty failure."""
    started = asyncio.Event()

    async def slow_tool() -> str:
        started.set()
        await asyncio.Event().wait()
        return "finished"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    toolkit = Toolkit(name="slow", tools=[slow_tool])
    bind_toolkit_authority(toolkit, authored_name="slow")
    owner = build_execution_identity_from_runtime_context(context)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", context.config, paths, owner)

    storage = storage_factory()
    agent = Agent(id="leader", model=model, tools=[toolkit], db=storage)

    @owned_tool_execution()
    async def reply() -> RunOutput:
        set_consumption_storage(storage_factory)
        return await agent.arun("start", session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(asyncio.Event()):
            running = asyncio.create_task(reply())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            (job,) = runtime.retained_jobs()
            await runtime.request_cancel(job.job_id)
            response = await asyncio.wait_for(running, JOB_TEST_TIMEOUT)
        [message] = [message for message in response.messages or [] if message.role == "tool"]
        assert "cancelled" in str(message.content)
        assert "do not call it again" in str(message.content)
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_finished_call_counts_as_read_when_its_reply_fails_on_the_next_step(
    tmp_path: Path,
    stream: bool,
) -> None:
    """A call that finished inside its reply is read once the failed run is saved, streaming or not."""

    async def report(topic: str) -> str:
        return f"report on {topic}"

    class FailingSecondStep(DelegationModel):
        steps: int = 0

        async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
            self.steps += 1
            if self.steps == 2:
                msg = "provider overloaded"
                raise RuntimeError(msg)
            return await super().ainvoke(*args, **kwargs)

        async def ainvoke_stream(self, *args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
            yield await self.ainvoke(*args, **kwargs)

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = FailingSecondStep(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("report", "call", topic="alpha")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", context.config, paths, owner)

    storage = storage_factory()
    agent = Agent(id="leader", model=model, tools=[assembled_function(report)], db=storage, telemetry=False)

    @owned_tool_execution()
    async def reply() -> None:
        set_consumption_storage(storage_factory)
        if stream:
            async for _event in agent.arun("Go", session_id=context.session_id, stream=True):
                pass
        else:
            await agent.arun("Go", session_id=context.session_id)

    try:
        with tool_runtime_context(context), suppress(RuntimeError):
            await reply()
        (job,) = runtime.retained_jobs()
        assert job.status == "completed"
        assert job.consumed
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_whose_wait_is_refused_leaves_its_job_to_be_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call whose wait the runtime refuses fails, and gives its job back, so the job's outcome still gets reported."""
    gate = asyncio.Event()

    async def slow_tool() -> str:
        await gate.wait()
        return "finished"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    async def refuse_wait(*_args: object, **_kwargs: object) -> Never:
        msg = "Tool job is not available in this conversation."
        raise JobAccessError(msg)

    monkeypatch.setattr(runtime, "wait", refuse_wait)
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("slow_tool", "call")]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[assembled_function(slow_tool)])
    key = ConversationKey(
        recipient=context.recipient,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        requester_id=context.requester_id,
        silent=False,
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await agent.arun("Go", session_id=context.session_id)
        assert response.tools is not None
        assert response.tools[0].tool_call_error
        (job,) = runtime.retained_jobs()
        gate.set()
        await wait_for_status(runtime, job.job_id, "completed")
        assert [ended.job_id for ended in await runtime.ended_jobs(key)] == [job.job_id]
    finally:
        gate.set()
        await runtime.shutdown()
