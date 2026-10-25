"""Managed workflows retain independent completion ownership for each participant call."""

from __future__ import annotations

import asyncio
import json
import threading
from functools import partial
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import build_agent_toolkit
from mindroom.config.models import ToolConfigEntry
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import defer_execution_cleanup, execution_resources
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import ScriptedAgents
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.test_dynamic_workflows import _make_context, _workflow_spec
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    call_job_tool,
    cancel_settled,
    job_actor,
    job_tool,
    run_job_turn,
    tool_job_journal,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_jobs.execution_authority import JobTool


def _workflow_caller(toolkit: DynamicWorkflowTools, config: Config) -> JobActor:
    """The general agent offering its workflow toolkit, granted as the agent is now."""
    tools = toolkit_job_tools(toolkit, authored_name="dynamic_workflow", snapshot=authority_snapshot(config, "general"))
    return job_actor(*tools.values(), name="general")


def _child_outputs(child_model: ScriptedModel) -> list[object]:
    """Return what each participant call returned, as the participant's last request carried them."""
    messages = child_model.requests[-1].messages
    return [json.loads(str(message.content))["result"] for message in messages if message.role == "tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_workflow_participant_runs_multiple_sync_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    parallel: bool,
) -> None:
    """Real subagent participants retain all calculator results in one managed workflow."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = managed
    # A participant can use only tools its caller has.
    context.config.agents["general"].tools = [
        ToolConfigEntry(name="dynamic_workflow", overrides={"allowed_tools": ["calculator"]}),
        ToolConfigEntry(name="calculator"),
    ]
    runtime = await tool_job_runtime(context.runtime_paths.storage_root)
    if managed:
        pin_background_tool_jobs(context.config, context.runtime_paths)
        register_background_runtime(context.runtime_paths, runtime)
    requested = [tool_call("add", "a", a=1, b=2), tool_call("multiply", "b", a=3, b=4)]
    child_model = ScriptedModel(
        [
            *([calls(*requested)] if parallel else [calls(call) for call in requested]),
            answer("workflow done"),
        ],
    )
    ScriptedAgents({"general": child_model}).install(monkeypatch)
    toolkit = DynamicWorkflowTools()
    caller = _workflow_caller(toolkit, context.config)
    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": ["calculator"]}],
        permissions={"models": ["claude-sonnet-5"], "tools": ["calculator"]},
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                assert json.loads(toolkit.create_workflow(spec))["status"] == "ok"
                outcome = await call_job_tool(
                    caller,
                    "run_workflow",
                    {"workflow_id": "competitor-research-report", "input": {"topic": "test"}},
                    call_id="outer",
                    run_id="root",
                    session_id=context.session_id,
                )
        assert not outcome.is_error, outcome.content
        assert _child_outputs(child_model) == [3, 12]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_workflow_participant_tools_run_under_their_own_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A participant is a subagent built with its own grants, so each of its calls is checked as itself."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = True
    # A participant can use only tools its caller has.
    context.config.agents["general"].tools = [
        ToolConfigEntry(name="dynamic_workflow", overrides={"allowed_tools": ["calculator"]}),
        ToolConfigEntry(name="calculator"),
    ]
    checked: list[tuple[str, dict[str, object]]] = []

    def authorize_execution(_owner: object, tool: JobTool, arguments: Mapping[str, object]) -> None:
        checked.append((tool.function.name, dict(arguments)))
        if "scope" not in tool.authority:
            msg = "Tool execution is no longer authorized for this caller."
            raise JobAccessError(msg)

    runtime = await tool_job_runtime(context.runtime_paths.storage_root, authorize_execution=authorize_execution)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    child_model = ScriptedModel(
        [
            calls(tool_call("add", "a", a=1, b=2)),
            calls(tool_call("multiply", "b", a=3, b=4)),
            answer("workflow done"),
        ],
    )
    ScriptedAgents({"general": child_model}).install(monkeypatch)
    toolkit = DynamicWorkflowTools()
    caller = _workflow_caller(toolkit, context.config)
    arguments = {"workflow_id": "competitor-research-report", "input": {"topic": "test"}}
    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": ["calculator"]}],
        permissions={"models": ["claude-sonnet-5"], "tools": ["calculator"]},
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                assert json.loads(toolkit.create_workflow(spec))["status"] == "ok"
                await call_job_tool(
                    caller,
                    "run_workflow",
                    arguments,
                    call_id="outer",
                    run_id="root",
                    session_id=context.session_id,
                )
        assert _child_outputs(child_model) == [3, 12]
        assert {check[0] for check in checked} == {"run_workflow", "add", "multiply"}
        assert ("run_workflow", arguments) in checked
        assert ("add", {"a": 1, "b": 2}) in checked
        assert ("multiply", {"a": 3, "b": 4}) in checked
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_cancel_composite_job_drains_all_sync_children(
    tmp_path: Path,
    parallel: bool,
) -> None:
    """Cancellation keeps resources until every started nested thread has exited."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = True
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(context.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, context.runtime_paths)
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

    nested = job_actor(*toolkit_job_tools(Toolkit(name="nested", tools=[leaf]), authored_name="nested").values())
    requested = [tool_call("leaf", str(value), value=value) for value in (1, 2)]
    child_model = ScriptedModel(
        [
            calls(tool_call("leaf", "0", value=0)),
            *([calls(*requested)] if parallel else [calls(call) for call in requested]),
            answer("done"),
        ],
    )

    async def cleanup() -> None:
        cleaned.set()

    async def composite() -> str:
        # A nested agent runs its own loop inside the job, its calls admitted under the job's ownership.
        result = await run_job_turn(nested, child_model, "Use the tools.", session_id="child")
        return str(result.content)

    model = ScriptedModel([calls(tool_call("composite", "outer", background_after=0)), answer("done")])
    actor = job_actor(job_tool(composite), name="general")

    @owned_tool_execution()
    async def run() -> str:
        result = await run_job_turn(actor, model, "Start the composite tool.", session_id=context.session_id)
        assert defer_execution_cleanup(cleanup)
        return json.loads(str(result.tools[0].result))["job_id"]

    try:
        with tool_runtime_context(context):
            job_id = await run()
            await asyncio.wait_for(started[0].wait(), JOB_TEST_TIMEOUT)
            if parallel:
                await asyncio.wait_for(started[1].wait(), JOB_TEST_TIMEOUT)
            waiter = asyncio.create_task(cancel_settled(runtime, job_id, owner=owner, depth=0))
            await wait_for_status(runtime, job_id, "cancel_requested")
            assert not cleaned.is_set()
            done, _ = await asyncio.wait({waiter}, timeout=0.05)
            assert not done
            assert completed == [0]
            release.set()
            await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
            assert cleaned.is_set()
            assert sorted(completed) == ([0, 1, 2] if parallel else [0, 1])
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_workflow_participant_tools_pass_the_job_runtimes_own_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A participant is granted by its caller's workflow tool, not by a delegate list, which stays empty here."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = True
    context.config.agents["general"].tools = [
        ToolConfigEntry(name="dynamic_workflow", overrides={"allowed_tools": ["calculator"]}),
        ToolConfigEntry(name="calculator"),
    ]
    assert context.config.agents["general"].delegate_to == []
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=context.runtime_paths,
        config_provider=lambda: context.config,
        bot_provider=lambda _name: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, context.runtime_paths.storage_root),
    )
    child_model = ScriptedModel([calls(tool_call("add", "a", a=1, b=2)), answer("workflow done")])
    ScriptedAgents({"general": child_model}).install(monkeypatch)
    entry = context.config.agents["general"].tools[0]
    assert isinstance(entry, ToolConfigEntry)
    toolkit = build_agent_toolkit(
        "dynamic_workflow",
        agent_name="general",
        config=context.config,
        runtime_paths=context.runtime_paths,
        worker_tools=[],
        runtime_overrides=None,
        tool_config_overrides=context.config.resolve_entity("general").authored_tool_configs[0].tool_config_overrides,
        execution_identity=build_execution_identity_from_runtime_context(context),
    )
    assert isinstance(toolkit, DynamicWorkflowTools)
    caller = _workflow_caller(toolkit, context.config)
    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": ["calculator"]}],
        permissions={"models": ["claude-sonnet-5"], "tools": ["calculator"]},
    )
    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(context):
                assert json.loads(toolkit.create_workflow(spec))["status"] == "ok"
                arguments = {"workflow_id": "competitor-research-report", "input": {"topic": "test"}}
                await call_job_tool(
                    caller,
                    "run_workflow",
                    arguments,
                    call_id="outer",
                    run_id="root",
                    session_id=context.session_id,
                )
        assert _child_outputs(child_model) == [3]
    finally:
        await coordinator.stop()
