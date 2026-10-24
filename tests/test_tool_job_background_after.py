"""Shared framework wait metadata remains outside application arguments."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.llm.provider import ToolSpec
from mindroom.llm.runs import RunStatus
from mindroom.participation import ParticipationGate
from mindroom.tool_jobs.background_after import BACKGROUND_AFTER_SCHEMA
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.execution import project_definitions, wait_mode
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import encode_tool_result
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.agent_tool_calls import cli_dispatch_scope
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    call_job_tool,
    job_actor,
    job_tool,
    run_job_turn,
    tool_job_runtime,
    toolkit_job_tools,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_jobs.execution_authority import JobTool
    from mindroom.tool_system.invocation import ToolCall, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from tests.tool_job_helpers import ProcessRuntime


async def _managed_context(tmp_path: Path) -> tuple[ToolRuntimeContext, ProcessRuntime]:
    """A reply's context in a conversation whose calls can become jobs, with that runtime registered."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    return context, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1])
async def test_application_background_after_collision_fails_before_execution(tmp_path: Path, depth: int) -> None:
    """An application argument cannot silently become runtime metadata, including in nested calls."""
    invoked = []

    async def application(background_after: int = 7) -> str:
        invoked.append(background_after)
        return str(background_after)

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    actor = job_actor(job_tool(application), depth=depth)
    model = ScriptedModel([calls(tool_call("application", "collision", background_after=None)), answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(actor, model)
        [schema] = model.requests[0].tools
        assert schema.parameters["properties"]["background_after"]["type"] == "integer"
        [tool] = record.tools
        assert tool.is_error
        assert "exclude_toolkits" in str(tool.result)
        assert invoked == []
        assert await runtime.list_jobs(owner=owner, depth=depth) == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shared_schema_adds_optional_wait_without_changing_application_schema(tmp_path: Path) -> None:
    """A managed function's definition offers the optional wait budget; its own schema and the job controls' stay."""

    def application(value: str) -> str:
        return value

    context, runtime = await _managed_context(tmp_path)
    controls = toolkit_job_tools(
        JobTools(context.runtime_paths, build_execution_identity_from_runtime_context(context)),
        authored_name="job",
    )
    tool = job_tool(application)
    actor = job_actor(tool, *controls.values())
    model = ScriptedModel([answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                await run_job_turn(actor, model)
        offered = {definition.name: definition for definition in model.requests[0].tools}
        schema = offered["application"].parameters
        assert schema["properties"]["background_after"] == BACKGROUND_AFTER_SCHEMA
        assert "background_after" not in schema["required"]
        assert "background_after" not in tool.function.parameters["properties"]
        # The job controls keep their own wait_timeout, and gain no background_after.
        control_schema = offered["job"].parameters
        assert control_schema == controls["job"].function.parameters
        assert "background_after" not in control_schema["properties"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_the_participation_decision_offers_the_wait_budget_the_reply_offers(tmp_path: Path) -> None:
    """The job definitions project before the participation gate decides, so the decision sends the reply's tools.

    The decision is the reply's request plus the decision prompt, so with job starts enabled both carry the
    optional ``background_after`` of the actor's managed function.
    """

    def application(value: str) -> str:
        return value

    context, runtime = await _managed_context(tmp_path)
    actor = job_actor(job_tool(application))
    model = ScriptedModel([answer('{"action":"respond","reason":"Asked to start."}'), answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                await run_job_turn(actor, model, participation=ParticipationGate())
        decision, reply = model.requests
        assert decision.tools == reply.tools
        [schema] = reply.tools
        assert schema.parameters["properties"]["background_after"] == BACKGROUND_AFTER_SCHEMA
    finally:
        await runtime.shutdown()


def test_a_strict_definition_requires_the_wait_budget_and_a_declared_one_stays() -> None:
    """A strict schema requires every property; a function's own background_after keeps its authored schema."""
    strict = ToolSpec(name="strict", description=None, parameters={"type": "object", "properties": {}}, strict=True)
    declared = ToolSpec(
        name="declared",
        description=None,
        parameters={"type": "object", "properties": {"background_after": {"type": "integer"}}},
    )

    projected = project_definitions((strict, declared), {"strict", "declared"})

    assert projected[0].parameters == {
        "type": "object",
        "properties": {"background_after": BACKGROUND_AFTER_SCHEMA},
        "required": ["background_after"],
    }
    assert projected[1] is declared
    assert strict.parameters == {"type": "object", "properties": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"background_after": None}, {"background_after": 0}, {"background_after": 0.01}],
)
async def test_wait_metadata_never_reaches_callable_or_hook(tmp_path: Path, arguments: dict[str, object]) -> None:
    """A real no-keyword callable executes once and bounded waits return without cancellation."""
    invoked = []
    hooked = []
    gate, entered = asyncio.Event(), asyncio.Event()

    async def application() -> str:
        invoked.append(True)
        entered.set()
        await gate.wait()
        return "finished"

    async def before(call: ToolCall, call_next: ToolNext) -> object:
        hooked.append(call.arguments)
        return await call_next(call.arguments)

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    actor = job_actor(job_tool(application))
    sent = dict(arguments)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                waiting = asyncio.create_task(call_job_tool(actor, "application", sent, middleware=(before,)))
                if arguments.get("background_after") is None:
                    await asyncio.wait_for(entered.wait(), JOB_TEST_TIMEOUT)
                    assert not waiting.done()
                    gate.set()
                    outcome = await asyncio.wait_for(waiting, JOB_TEST_TIMEOUT)
                    assert outcome.content == "finished"
                else:
                    outcome = await asyncio.wait_for(waiting, JOB_TEST_TIMEOUT)
                    handle = json.loads(outcome.content)
                    assert handle["status"] == "running"
                    gate.set()
                    completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
                    assert completed.job.result == "finished"
                assert invoked == [True]
                assert hooked == [{}]
                assert sent == arguments
                jobs = await runtime.list_jobs(owner=owner, depth=0)
                assert jobs[0].adapter["arguments"] == encode_tool_result(arguments)
    finally:
        gate.set()
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

    context, runtime = await _managed_context(tmp_path)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(
                    job_actor(job_tool(application)),
                    "application",
                    {"background_after": budget},
                )
        assert outcome.is_error
        assert "background_after" in outcome.content
        assert invoked == []
        assert await runtime.list_jobs(owner=build_execution_identity_from_runtime_context(context), depth=0) == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_batch_tools_keep_independent_wait_budgets(tmp_path: Path) -> None:
    """A detached call cannot block a sibling call or lose its original arguments."""
    gate = asyncio.Event()
    invoked = []

    async def slow(value: str) -> str:
        invoked.append(value)
        await gate.wait()
        return value

    async def fast(value: str) -> str:
        invoked.append(value)
        return value

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    model = ScriptedModel(
        [
            calls(
                tool_call("slow", "slow-call", value="slow-result", background_after=0),
                tool_call("fast", "fast-call", value="fast-result", background_after=None),
            ),
            answer("ready"),
        ],
    )
    plugin = Toolkit(name="plugin", tools=[slow, fast])
    actor = job_actor(*toolkit_job_tools(plugin, authored_name="plugin").values())
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await asyncio.wait_for(run_job_turn(actor, model), JOB_TEST_TIMEOUT)
                results = {tool.tool_name: tool for tool in record.tools}
                handle = json.loads(str(results["slow"].result))
                assert handle["status"] == "running"
                assert results["fast"].result == "fast-result"
                assert results["slow"].tool_args == {"value": "slow-result", "background_after": 0}
                gate.set()
                completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
                assert completed.job.result == "slow-result"
        assert sorted(invoked) == ["fast-result", "slow-result"]
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"background_after": None}, {"background_after": 0}, {"background_after": 1}],
)
async def test_owned_nested_application_cannot_create_detached_job(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    """Nested execution stays in its outer job; advertised or explicit detach budgets cannot be ignored."""
    called = []

    async def application() -> str:
        called.append(True)
        return "inline result"

    context, runtime = await _managed_context(tmp_path)
    model = ScriptedModel([calls(tool_call("application", "call", **arguments)), answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context), job_control_context(JobControl()):
                record = await run_job_turn(job_actor(job_tool(application)), model)
        assert "background_after" not in model.requests[0].tools[0].parameters["properties"]
        [tool] = record.tools
        if arguments.get("background_after") is None:
            assert not tool.is_error
            assert tool.result == "inline result"
            assert called == [True]
        else:
            assert tool.is_error
            assert "outer job" in str(tool.result)
            assert called == []
        owner = build_execution_identity_from_runtime_context(context)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
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
    nested: bool,
    invalid_budget: object,
) -> None:
    """A batch reports one tool failure and keeps its siblings and the correction turn alive."""
    invoked: list[str] = []

    async def application(value: str) -> str:
        invoked.append(value)
        return value

    context, runtime = await _managed_context(tmp_path)
    model = ScriptedModel(
        [
            calls(
                tool_call("application", "invalid", value="must-not-run", background_after=invalid_budget),
                tool_call("application", "sibling", value="sibling", background_after=None),
            ),
            calls(tool_call("application", "corrected", value="corrected", background_after=None)),
            answer("done"),
        ],
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context), job_control_context(JobControl()) if nested else nullcontext():
                record = await run_job_turn(job_actor(job_tool(application)), model)
        assert record.status is RunStatus.completed
        assert invoked == ["sibling", "corrected"]
        rejected = next(tool for tool in record.tools if tool.tool_call_id == "invalid")
        assert rejected.is_error
        assert "background_after" in str(rejected.result)
        assert rejected.tool_args == {"value": "must-not-run", "background_after": invalid_budget}
        assert any(
            message.tool_call_id == "invalid" and "background_after" in str(message.content)
            for message in model.requests[1].messages
        )
        jobs = await runtime.list_jobs(owner=build_execution_identity_from_runtime_context(context), depth=0)
        assert len(jobs) == (0 if nested else 2)
        assert all(job.adapter["tool_call_id"] != "invalid" for job in jobs)
    finally:
        await runtime.shutdown()


class _ConnectedTools(Toolkit):
    """A toolkit the SDK connects for one run, like Postgres."""

    _requires_connect = True

    def __init__(self) -> None:
        super().__init__(name="connected", tools=[self.query])

    def query(self) -> str:
        """Return synthetic rows."""
        return "rows"


class MCPTools(Toolkit):
    """Agno recognizes its MCP toolkits by this class name."""

    def __init__(self) -> None:
        super().__init__(name="mcp", tools=[self.query])

    def query(self) -> str:
        """Return synthetic rows."""
        return "rows"


def _excluded_tool() -> JobTool:
    """A function of a toolkit built as shell, which the test settings exclude."""
    toolkit = Toolkit(name="shell", tools=[_ordinary])
    bind_toolkit_construction(toolkit, ToolConstruction("shell", None))
    return toolkit_job_tools(toolkit, authored_name="shell")["_ordinary"]


def _ordinary() -> str:
    return "ordinary"


def _stopping_tool() -> JobTool:
    tool = job_tool(_ordinary)
    return replace(tool, function=replace(tool.function, stop_after_call=True))


@pytest.mark.parametrize(
    ("case", "depth", "owned", "expected"),
    [
        ("ordinary", 0, False, "managed"),
        ("ordinary", 1, False, "inline"),
        ("ordinary", 0, True, "inline"),
        ("stops_step", 0, False, "inline"),
        ("job", 0, False, "native"),
        ("job", 1, True, "native"),
        ("run_connection", 0, False, "native"),
        ("mcp", 0, False, "native"),
        ("excluded", 0, False, "native"),
    ],
)
def test_wait_mode_classifies_every_call_from_current_policy(
    tmp_path: Path,
    case: str,
    depth: int,
    owned: bool,
    expected: str,
) -> None:
    """One classifier decides definition projection and execution for each kind of assembled function.

    A function the framework generated is never classified; ``test_tool_job_sdk_functions.py`` covers it.
    """
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(exclude_toolkits=["shell"]),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    context = _delegate_runtime_context(config, paths)
    tools = {
        "ordinary": lambda: job_tool(_ordinary),
        "stops_step": _stopping_tool,
        "job": lambda: toolkit_job_tools(
            JobTools(paths, build_execution_identity_from_runtime_context(context)),
            authored_name="job",
        )["job"],
        "run_connection": lambda: toolkit_job_tools(_ConnectedTools(), authored_name="connected")["query"],
        "mcp": lambda: toolkit_job_tools(MCPTools(), authored_name="mcp")["query"],
        "excluded": _excluded_tool,
    }
    tool = tools[case]()
    with tool_runtime_context(context), job_control_context(JobControl()) if owned else nullcontext():
        assert wait_mode(tool, depth=depth) == expected


def test_a_call_a_cli_command_makes_runs_inline(tmp_path: Path) -> None:
    """A CLI call returns to its shell command, whose saved result is the reply's, so it never becomes a job."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    tool = job_tool(_ordinary)
    with tool_runtime_context(context):
        assert wait_mode(tool, depth=0) == "managed"
        with cli_dispatch_scope():
            assert wait_mode(tool, depth=0) == "inline"


@pytest.mark.asyncio
@pytest.mark.parametrize("toolkit", [_ConnectedTools, MCPTools])
async def test_run_connected_toolkits_run_inline_without_wait_metadata(
    tmp_path: Path,
    toolkit: type[Toolkit],
) -> None:
    """A connection opened for one run is never detached, so its calls neither advertise nor accept a wait budget."""
    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    actor = job_actor(*toolkit_job_tools(toolkit(), authored_name="connected").values())
    model = ScriptedModel(
        [
            calls(
                tool_call("query", "connected"),
                # A stale budget is rejected rather than silently running the call without detaching it.
                tool_call("query", "stale", background_after=0),
            ),
            answer("done"),
        ],
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(actor, model)
        assert "background_after" not in model.requests[0].tools[0].parameters.get("properties", {})
        results = {tool.tool_call_id: tool for tool in record.tools}
        assert not results["connected"].is_error
        assert results["connected"].result == "rows"
        assert results["stale"].is_error
        assert "background_after" in str(results["stale"].result)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await runtime.shutdown()
