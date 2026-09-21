"""Shared framework wait metadata remains outside application arguments."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool annotations at runtime.
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.media import Image
from agno.models.fallback import FallbackConfig
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunContentEvent, RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall, ToolResult

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.agno_execution import _drain_result
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import decode_tool_result, encode_tool_result
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1])
async def test_application_wait_timeout_collision_fails_before_execution(tmp_path: Path, depth: int) -> None:
    """An application argument cannot silently become runtime metadata, including in nested calls."""
    invoked = []

    async def application(wait_timeout: int = 7) -> str:
        invoked.append(wait_timeout)
        return str(wait_timeout)

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model, depth=depth)
    function = Function.from_callable(application)
    function._agent = Agent(id="leader", model=model)
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                with pytest.raises(ValueError, match="exclude_toolkits"):
                    model._format_tools([function])
                success, _, call, result = await model.arun_function_call(
                    FunctionCall(function=function, call_id="collision", arguments={"wait_timeout": None}),
                )
        assert success is False
        assert result.status == "failure"
        assert "exclude_toolkits" in call.error
        assert invoked == []
        assert await runtime.list_jobs(owner=owner, depth=depth) == []
        assert function.parameters["properties"]["wait_timeout"]["type"] == "integer"
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_shared_schema_adds_optional_wait_without_changing_application_schema(
    tmp_path: Path,
    fallback: bool,
) -> None:
    """Concrete fallback and primary schemas expose framework metadata for arbitrary Functions."""

    def application(value: str) -> str:
        return value

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model, backup = DelegationModel(id="primary"), DelegationModel(id="backup")
    install_tool_job_execution(model, FallbackConfig(on_error=[backup]))
    function = Function.from_callable(application)
    function._agent = Agent(id="leader", model=model)
    try:
        with tool_runtime_context(context):
            formatted = (backup if fallback else model)._format_tools([function])
        schema = formatted[0]["function"]["parameters"]
        assert schema["properties"]["wait_timeout"]["anyOf"] == [{"type": "number", "minimum": 0}, {"type": "null"}]
        assert "wait_timeout" not in schema["required"]
        assert "wait_timeout" not in function.parameters["properties"]
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"wait_timeout": None}, {"wait_timeout": 0}, {"wait_timeout": 0.01}])
async def test_wait_metadata_never_reaches_callable_or_hook(tmp_path: Path, arguments: dict[str, object]) -> None:
    """A real no-keyword callable executes once and bounded waits return without cancellation."""
    invoked = []
    hooked = []
    gate = asyncio.Event()

    async def application() -> str:
        invoked.append(True)
        await gate.wait()
        return "finished"

    async def before(fc: FunctionCall) -> None:
        hooked.append(fc.arguments)

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(application)
    function.pre_hook = before
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state={})
    call = FunctionCall(function=function, call_id="call", arguments=arguments)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                waiting = asyncio.create_task(model.arun_function_call(call))
                if arguments.get("wait_timeout") is None:
                    await asyncio.sleep(0.03)
                    assert not waiting.done()
                    gate.set()
                    result = await asyncio.wait_for(waiting, 1)
                    assert result[3].result == "finished"
                else:
                    result = await asyncio.wait_for(waiting, 1)
                    handle = json.loads(result[3].result)
                    assert handle["status"] == "running"
                    gate.set()
                    completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
                    assert completed.job.result == "finished"
                assert invoked == [True]
                assert hooked == [{}]
                assert call.arguments == arguments
                jobs = await runtime.list_jobs(owner=owner, depth=0)
                assert decode_tool_result(jobs[0].adapter["arguments"]) == arguments
    finally:
        gate.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget",
    [
        -1,
        True,
        "1",
        float("nan"),
        float("inf"),
        pytest.param(10**400, id="overflow"),
        pytest.param(-(10**400), id="negative-overflow"),
    ],
)
async def test_invalid_wait_budget_never_starts_application(tmp_path: Path, budget: object) -> None:
    """Reject malformed framework metadata before accepting any execution."""
    invoked = []

    async def application() -> str:
        invoked.append(True)
        return "finished"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(application)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                call = FunctionCall(function=function, call_id="call", arguments={"wait_timeout": budget})
                success, _, returned_call, result = await model.arun_function_call(call)
                assert success is False
                assert returned_call is call
                assert result.status == "failure"
                assert "wait_timeout" in call.error
                assert result.error == call.error
                assert call.arguments == {"wait_timeout": budget}
        assert invoked == []
        assert await runtime.list_jobs(owner=build_execution_identity_from_runtime_context(context), depth=0) == []
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


def test_tuple_codec_preserves_nested_sequence_types() -> None:
    """Durable results cannot change tuples into application-visible lists."""
    expected = {"items": [(1, "two"), ([], (3,))]}
    assert decode_tool_result(json.loads(json.dumps(encode_tool_result(expected)))) == expected


@pytest.mark.asyncio
async def test_stream_replay_retains_individual_rich_metadata() -> None:
    """Replay must retain each rich chunk rather than flattening it into text."""
    first = ToolResult(content="first", metadata={"artifact": "one"})
    second = ToolResult(content="second", metadata={"artifact": "two"})
    value, _, replay = await _drain_result(iter([first, second]))
    restored = decode_tool_result(json.loads(json.dumps(encode_tool_result(replay))))
    assert restored == [{"result": first}, {"result": second}]
    assert value.content == "firstsecond"
    assert value.metadata == {"artifact": "two"}


@pytest.mark.asyncio
async def test_batch_tools_keep_independent_wait_budgets(tmp_path: Path) -> None:
    """A detached plugin Function cannot block a sibling tool or lose its original args."""
    gate = asyncio.Event()
    calls = []

    async def slow(value: str) -> str:
        calls.append(value)
        await gate.wait()
        return value

    async def fast(value: str) -> str:
        calls.append(value)
        return value

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(
                tool_calls=[
                    _call("slow", "slow-call", value="slow-result", wait_timeout=0),
                    _call("fast", "fast-call", value="fast-result", wait_timeout=None),
                ],
            ),
            ModelResponse(content="ready"),
        ],
    )
    install_tool_job_execution(model)
    plugin = Toolkit(name="plugin", tools=[Function.from_callable(slow), Function.from_callable(fast)])
    agent = Agent(id="leader", model=model, tools=[plugin])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await asyncio.wait_for(agent.arun("start", session_id=context.session_id), 1)
                results = {tool.tool_name: tool for tool in response.tools}
                handle = json.loads(results["slow"].result)
                assert handle["status"] == "running"
                assert results["fast"].result == "fast-result"
                assert results["slow"].tool_args == {"value": "slow-result", "wait_timeout": 0}
                gate.set()
                completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
                assert completed.job.result == "slow-result"
        assert sorted(calls) == ["fast-result", "slow-result"]
    finally:
        gate.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_rich_stream_reaches_sdk_with_media_metadata_and_events(tmp_path: Path) -> None:
    """Draining and replaying rich streams preserves the SDK result and event ordering."""

    async def generated() -> AsyncIterator[RunContentEvent | ToolResult | str]:
        yield RunContentEvent(content="event")
        yield ToolResult(content="picture", images=[Image(content=b"image")], metadata={"source": "retained"})
        yield "tail"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(generated)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                success, _, call, result = await model.arun_function_call(
                    FunctionCall(function=function, call_id="call", arguments={}),
                )
        assert success is True
        assert result.result.metadata == {"source": "retained"}
        assert result.result.content == "eventpicturetail"
        assert result.images[0].content == b"image"
        replayed = list(call.result)
        assert isinstance(replayed[0], RunContentEvent)
        assert replayed[0].content == "event"
        assert replayed[1:] == ["picture", "tail"]
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"wait_timeout": None}, {"wait_timeout": 0}, {"wait_timeout": 1}])
async def test_owned_nested_application_cannot_create_detached_job(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    """Nested execution stays in its outer job; advertised or explicit detach budgets cannot be ignored."""
    called = []

    async def application() -> str:
        called.append(True)
        return "inline result"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    function = Function.from_callable(application)
    function._agent = Agent(id="leader")
    function._run_context = RunContext(run_id="run", session_id=context.session_id, session_state={})
    try:
        async with execution_resources():
            with tool_runtime_context(context), job_control_context(JobControl()):
                schema = model._format_tools([function])[0]["function"]["parameters"]
                assert "wait_timeout" not in schema["properties"]
                call = FunctionCall(function=function, call_id="call", arguments=arguments)
                if arguments.get("wait_timeout") is None:
                    success, _, _, result = await model.arun_function_call(call)
                    assert success is True
                    assert result.result == "inline result"
                    assert called == [True]
                else:
                    success, _, returned_call, result = await model.arun_function_call(call)
                    assert success is False
                    assert returned_call is call
                    assert result.status == "failure"
                    assert "outer job" in call.error
                    assert called == []
        owner = build_execution_identity_from_runtime_context(context)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("nested", "invalid_budget"),
    [
        pytest.param(False, "bad", id="invalid-type"),
        pytest.param(False, 10**400, id="overflow"),
        pytest.param(True, 0, id="nested"),
    ],
)
async def test_invalid_batched_wait_is_correctable_without_losing_siblings(
    tmp_path: Path,
    streaming: bool,
    nested: bool,
    invalid_budget: object,
) -> None:
    """Real SDK batches report one tool failure and keep siblings and correction turns alive."""
    invoked: list[str] = []

    async def application(value: str) -> str:
        invoked.append(value)
        return value

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(
                tool_calls=[
                    _call("application", "invalid", value="must-not-run", wait_timeout=invalid_budget),
                    _call("application", "sibling", value="sibling", wait_timeout=None),
                ],
            ),
            ModelResponse(tool_calls=[_call("application", "corrected", value="corrected", wait_timeout=None)]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    agent = Agent(id="leader", model=model, tools=[application])
    try:
        async with execution_resources():
            with tool_runtime_context(context), job_control_context(JobControl()) if nested else nullcontext():
                if streaming:
                    events = [
                        event
                        async for event in agent.arun(
                            "start",
                            session_id=context.session_id,
                            stream=True,
                            stream_events=True,
                            yield_run_output=True,
                        )
                    ]
                    assert not any(isinstance(event, RunErrorEvent) for event in events)
                    response = next(event for event in reversed(events) if isinstance(event, RunOutput))
                else:
                    response = await agent.arun("start", session_id=context.session_id)
        assert response.status == RunStatus.completed
        assert invoked == ["sibling", "corrected"]
        rejected = next(tool for tool in response.tools if tool.tool_call_id == "invalid")
        assert rejected.tool_call_error
        assert "wait_timeout" in rejected.result
        assert rejected.tool_args == {"value": "must-not-run", "wait_timeout": invalid_budget}
        assert any(
            message.tool_call_id == "invalid" and "wait_timeout" in message.content for message in model.seen_messages
        )
        jobs = await runtime.list_jobs(owner=build_execution_identity_from_runtime_context(context), depth=0)
        assert len(jobs) == (0 if nested else 2)
        assert all(job.adapter["tool_call_id"] != "invalid" for job in jobs)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()
