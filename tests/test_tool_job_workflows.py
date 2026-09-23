"""Managed workflows retain independent completion ownership for each participant call."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.function import FunctionCall

from mindroom.config.models import ToolConfigEntry
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.hooks import HookRegistry
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.resources import defer_execution_cleanup, execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from tests.delegation_helpers import DelegationModel, _call
from tests.test_dynamic_workflows import _make_context, _workflow_spec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_workflow_participant_runs_multiple_sync_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    parallel: bool,
) -> None:
    """Real ephemeral participants retain all calculator results in one managed workflow."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = managed
    context.config.agents["general"].tools = [
        ToolConfigEntry(name="dynamic_workflow", overrides={"allowed_tools": ["calculator"]}),
    ]
    runtime = ToolJobRuntime(context.runtime_paths.storage_root)
    if managed:
        register_background_runtime(context.runtime_paths, runtime)
    calls = [_call("add", "a", a=1, b=2), _call("multiply", "b", a=3, b=4)]
    child_model = DelegationModel(
        id="test",
        responses=[
            *([ModelResponse(tool_calls=calls)] if parallel else [ModelResponse(tool_calls=[call]) for call in calls]),
            ModelResponse(content="workflow done"),
        ],
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.dynamic_workflow.model_loading.get_model_instance",
        lambda *_args, **_kwargs: child_model,
    )
    outer = DelegationModel(id="test")
    if managed:
        install_tool_job_execution(outer)
    toolkit = DynamicWorkflowTools()
    function = toolkit.get_async_functions()["run_workflow"]
    function._agent = Agent(id="general", telemetry=False)
    function._run_context = RunContext(run_id="root", session_id=context.session_id, session_state={})
    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "ephemeral_agent", "tools": ["calculator"]}],
        permissions={"models": ["claude-sonnet-5"], "tools": ["calculator"]},
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                assert json.loads(toolkit.create_workflow(spec))["status"] == "ok"
                result = await outer.arun_function_call(
                    FunctionCall(
                        function=function,
                        call_id="outer",
                        arguments={"workflow_id": "competitor-research-report", "input": {"topic": "test"}},
                    ),
                )
        assert result[0] is True
        outputs = [message.content for message in child_model.seen_messages if message.role == "tool"]
        assert [json.loads(output)["result"] for output in outputs] == [3, 12]
    finally:
        register_background_runtime(context.runtime_paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("async_hook", [False, True])
async def test_cancel_composite_job_drains_all_sync_children(  # noqa: PLR0915
    tmp_path: Path,
    parallel: bool,
    async_hook: bool,
) -> None:
    """Cancellation keeps resources until every started nested thread has exited."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = True
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(context.runtime_paths.storage_root)
    register_background_runtime(context.runtime_paths, runtime)
    loop = asyncio.get_running_loop()
    started = [asyncio.Event(), asyncio.Event()]
    release = threading.Event()
    completed: list[int] = []
    cleaned = asyncio.Event()

    def leaf(value: int) -> str:
        if value > 0:
            loop.call_soon_threadsafe(started[value - 1].set)
            assert release.wait(5), "test did not release its worker"
        completed.append(value)
        return str(value)

    toolkit = Toolkit(name="nested", tools=[leaf])
    prepend_tool_hook_bridge(toolkit, build_tool_hook_bridge(HookRegistry.empty(), agent_name="general"))
    if async_hook:

        async def around(func: Callable[..., Awaitable[str]], args: dict[str, object]) -> str:
            return await func(**args)

        toolkit.functions["leaf"].tool_hooks.append(around)
    calls = [_call("leaf", str(value), value=value) for value in (1, 2)]
    child_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("leaf", "0", value=0)]),
            *([ModelResponse(tool_calls=calls)] if parallel else [ModelResponse(tool_calls=[call]) for call in calls]),
            ModelResponse(content="done"),
        ],
    )
    child = Agent(id="general", model=child_model, tools=[toolkit], telemetry=False)

    async def cleanup() -> None:
        cleaned.set()

    async def composite() -> str:
        result = await child.arun("Use the tools.", session_id="child")
        return str(result.content)

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("composite", "outer", wait_timeout=0)]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    actor = Agent(id="general", model=model, tools=[composite], telemetry=False)

    @owned_tool_execution
    async def run() -> str:
        result = await actor.arun("Start the composite tool.", session_id=context.session_id)
        assert defer_execution_cleanup(cleanup)
        return json.loads(result.tools[0].result)["job_id"]

    try:
        with tool_runtime_context(context):
            job_id = await run()
            await asyncio.wait_for(started[0].wait(), 2)
            if parallel:
                await asyncio.wait_for(started[1].wait(), 2)
            cancelled = await runtime.cancel(job_id, owner=owner, depth=0)
            assert cancelled.status == "cancel_requested"
            assert not cleaned.is_set()
            waiter = asyncio.create_task(runtime.cancel(job_id, owner=owner, depth=0, await_completion=True))
            done, _ = await asyncio.wait({waiter}, timeout=0.05)
            assert not done
            assert completed == [0]
            release.set()
            await asyncio.wait_for(waiter, 2)
            assert cleaned.is_set()
            assert sorted(completed) == ([0, 1, 2] if parallel else [0, 1])
    finally:
        release.set()
        register_background_runtime(context.runtime_paths, None)
        await runtime.shutdown()
