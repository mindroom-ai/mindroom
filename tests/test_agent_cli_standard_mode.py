"""Standard-mode agents call their other tools from inside their own shell commands."""

from __future__ import annotations

import asyncio
import gc
import json
import re
import weakref
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run import RunContext  # noqa: TC002 - Agno injects the tool argument by its annotation
from agno.tools.toolkit import Toolkit

from mindroom import agents, ai
from mindroom.agent_cli.cli_shell_agent import STANDARD_CLI_NOTE, CliShellAgent, _cli_callable
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.access_schema_support import with_responder_access
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context
from tests.test_agent_cli_shell_access import running_api  # noqa: F401 - pytest fixture
from tests.test_delegation_execution import DelegationModel
from tests.tool_job_helpers import job_owner

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _helper_runtime(
    tmp_path: Path,
    registry: TurnToolRegistry,
    *,
    memory_backend: str = "file",
) -> ToolRuntimeContext:
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "calculator"],
        memory_backend=memory_backend,
        learning=False,
    )
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=registry))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    return runtime


async def _respond(runtime: ToolRuntimeContext) -> str:
    with tool_runtime_context(runtime):
        return await ai.ai_response(
            replace(_turn_context(), agent_mode="standard", run_id=None),
            prompt="Add two and three",
            runtime_paths=runtime.runtime_paths,
            config=runtime.config,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
            supports_native_tool_approval=True,
        )


def _tool_output(provider: ScriptedProvider) -> str:
    return next(str(message["content"]) for message in provider.requests[1]["messages"] if message["role"] == "tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_backend", ["file", "none"], ids=["workspace", "no-workspace"])
async def test_standard_shell_command_calls_another_tool_through_mindroom_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
    memory_backend: str,
) -> None:
    """A native shell command composes a native tool call, with or without an agent workspace; the grant ends with the response."""
    runtime = _helper_runtime(tmp_path, running_api, memory_backend=memory_backend)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    arguments = json.dumps({"a": 2, "b": 3})
    call_id = "00000000-0000-4000-8000-000000000001"
    command = (
        f"mindroom-agent tools call calculator add --call-id {call_id} --json '{arguments}'; "
        f"mindroom-agent calls wait {call_id}"
    )
    provider.steps = [[("run_shell_command", {"args": command})], "done"]

    assert "done" in await _respond(runtime)

    first = provider.requests[0]
    assert {"run_shell_command", "add"} <= {tool["function"]["name"] for tool in first["tools"]}
    assert STANDARD_CLI_NOTE in str(first["messages"])
    output = _tool_output(provider)
    assert '"status":"completed"' in output.replace(" ", ""), output
    assert "5" in output, output
    assert not running_api._owners


def _call_and_wait(call_id: str, toolkit: str, function: str, arguments: dict[str, object]) -> str:
    return (
        f"mindroom-agent tools call {toolkit} {function} --call-id {call_id} --json '{json.dumps(arguments)}'; "
        f"mindroom-agent calls wait {call_id}"
    )


@pytest.mark.asyncio
async def test_nested_cli_shell_call_runs_inside_the_outer_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """A shell call made through the CLI runs in the outer command's window, without waiting for it."""
    runtime = _helper_runtime(tmp_path, running_api)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    command = "echo OUTER=$MINDROOM_AGENT_CLI_WINDOW; " + _call_and_wait(
        "00000000-0000-4000-8000-000000000002",
        "shell",
        "run_shell_command",
        {"args": "echo NESTED=$MINDROOM_AGENT_CLI_WINDOW"},
    )
    provider.steps = [[("run_shell_command", {"args": command})], "done"]

    async with asyncio.timeout(60):
        assert "done" in await _respond(runtime)

    output = _tool_output(provider)
    assert '"status":"completed"' in output.replace(" ", ""), output
    outer = re.search(r"OUTER=(native-[0-9a-f]{32})", output)
    nested = re.search(r"NESTED=(native-[0-9a-f]{32})", output)
    assert outer is not None, output
    assert nested is not None, output
    assert nested.group(1) == outer.group(1)


@pytest.mark.asyncio
async def test_parallel_native_shell_calls_each_reach_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """Overlapping shell calls in one provider batch each reach the CLI, even after the other finished."""
    runtime = _helper_runtime(tmp_path, running_api)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [
        [
            (
                "run_shell_command",
                {
                    # The slow command still calls after the fast one finished, so admission must stay open for it.
                    "args": ("sleep 1; " if index == 1 else "")
                    + _call_and_wait(
                        f"00000000-0000-4000-8000-00000000001{index}",
                        "calculator",
                        "add",
                        {"a": index, "b": 10},
                    ),
                },
            )
            for index in (1, 2)
        ],
        "done",
    ]

    async with asyncio.timeout(60):
        assert "done" in await _respond(runtime)

    outputs = [str(message["content"]) for message in provider.requests[1]["messages"] if message["role"] == "tool"]
    assert len(outputs) == 2
    assert all('"status":"completed"' in output.replace(" ", "") for output in outputs), outputs
    assert any("11" in output for output in outputs), outputs
    assert any("12" in output for output in outputs), outputs


def _wait_for_peer(own: str, peer: str) -> str:
    return (
        f"touch {own}.ready; for i in $(seq 200); do test -f {peer}.ready && echo overlapped && exit 0; sleep 0.05; done; "
        "echo alone; exit 1"
    )


@pytest.mark.asyncio
async def test_native_shell_calls_in_one_batch_run_at_the_same_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """The CLI never makes parallel shell commands wait for each other: each one sees the other running."""
    runtime = _helper_runtime(tmp_path, running_api)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [
        [
            ("run_shell_command", {"args": _wait_for_peer("first", "second")}),
            ("run_shell_command", {"args": _wait_for_peer("second", "first")}),
        ],
        "done",
    ]

    async with asyncio.timeout(60):
        assert "done" in await _respond(runtime)

    outputs = [str(message["content"]) for message in provider.requests[1]["messages"] if message["role"] == "tool"]
    assert len(outputs) == 2
    assert all("overlapped" in output for output in outputs), outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("jobs", [False, True], ids=["paused-run", "job-approval"])
async def test_approval_gated_tools_stay_native_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
    *,
    jobs: bool,
) -> None:
    """A tool that may need approval keeps its native confirmation, or asks as its job, and is not offered to the CLI."""
    runtime = _helper_runtime(tmp_path, running_api)
    runtime.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="add", action="require_approval")],
    )
    runtime.config.background_tool_jobs = BackgroundToolJobsConfig(enabled=jobs)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [[("run_shell_command", {"args": "mindroom-agent tools list"})], "done"]

    assert "done" in await _respond(runtime)

    assert "add" in {tool["function"]["name"] for tool in provider.requests[0]["tools"]}
    listing = _tool_output(provider).replace(" ", "")
    assert '"function":"subtract"' in listing, listing
    assert '"function":"add"' not in listing, listing


def test_the_job_controls_are_not_offered_to_the_cli(tmp_path: Path) -> None:
    """A job read through the CLI is never saved as the reply's own call, so the reply reads its jobs directly."""
    job = JobTools(test_runtime_paths(tmp_path), job_owner()).get_async_functions()["job"]
    assert not _cli_callable(job)


@pytest.mark.parametrize("jobs", [False, True], ids=["paused-run", "job-approval"])
def test_shell_that_needs_approval_gets_no_cli(tmp_path: Path, *, jobs: bool) -> None:
    """An approved command resumes later, or runs in its job after the reply ended, without this response's CLI."""
    runtime = _helper_runtime(tmp_path, registry=SimpleNamespace())
    runtime.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
    )
    # With shell managed, its gated command asks for approval as its job.
    runtime.config.background_tool_jobs = BackgroundToolJobsConfig(enabled=jobs, exclude_toolkits=[])
    set_api_server_address("127.0.0.1", 8765)
    try:
        agent = agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            build_execution_identity_from_runtime_context(runtime),
            agent_cli_in_shell=True,
            supports_native_tool_approval=True,
        )
    finally:
        clear_api_server_address()

    try:
        assert not isinstance(agent, CliShellAgent)
        assert STANDARD_CLI_NOTE not in agent.instructions
    finally:
        close_agent_runtime_state_dbs(agent)


@pytest.mark.parametrize("managed", [False, True], ids=["excluded-shell", "managed-shell"])
def test_a_shell_that_runs_as_a_background_job_gets_no_cli(tmp_path: Path, *, managed: bool) -> None:
    """A shell command that can outlive its reply cannot use that reply's CLI, which ends with it."""
    runtime = _helper_runtime(tmp_path, registry=SimpleNamespace())
    runtime.config.background_tool_jobs = BackgroundToolJobsConfig(
        enabled=True,
        exclude_toolkits=[] if managed else ["shell"],
    )
    set_api_server_address("127.0.0.1", 8765)
    try:
        agent = agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            build_execution_identity_from_runtime_context(runtime),
            agent_cli_in_shell=True,
            supports_native_tool_approval=True,
        )
    finally:
        clear_api_server_address()

    try:
        assert isinstance(agent, CliShellAgent) is not managed
    finally:
        close_agent_runtime_state_dbs(agent)


@pytest.mark.parametrize(
    ("channel", "api_running", "response_turn"),
    [("matrix", False, True), ("openai_compat", True, True), ("matrix", True, False), ("matrix", True, True)],
)
def test_cli_shell_agent_needs_a_matrix_response_turn_and_the_running_api(
    tmp_path: Path,
    channel: str,
    *,
    api_running: bool,
    response_turn: bool,
) -> None:
    """Without the API server, outside Matrix, or outside a response turn (teams, calls), there is no CLI or note."""
    runtime = _helper_runtime(tmp_path, registry=SimpleNamespace())
    identity = replace(build_execution_identity_from_runtime_context(runtime), channel=channel)
    if api_running:
        set_api_server_address("127.0.0.1", 8765)
    try:
        agent = agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            identity,
            agent_cli_in_shell=response_turn,
        )
    finally:
        clear_api_server_address()

    try:
        eligible = channel == "matrix" and api_running and response_turn
        assert isinstance(agent, CliShellAgent) is eligible
        assert isinstance(agent, KnowledgeToolDescribingAgent)
        assert (STANDARD_CLI_NOTE in agent.instructions) is eligible
    finally:
        close_agent_runtime_state_dbs(agent)


@pytest.mark.asyncio
async def test_cli_calls_share_state_with_the_run_a_subagent_result_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """After a subagent returns, CLI calls from the continued run's shell share that run's state with its native calls."""
    runtime = _helper_runtime(tmp_path, running_api)
    # Delegation reads the managed runtime's spend monitor, which this harness leaves out.
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=running_api, budgets=None))
    runtime.config.agents["helper"].delegate_to = ["code"]
    runtime.config.agents["code"] = AgentConfig(display_name="Code", learning=False)
    with_responder_access(runtime.config, "code", users=[runtime.requester_id])
    persist_entity_accounts(runtime.config, runtime.runtime_paths)

    def remember(value: int, run_context: RunContext) -> str:
        run_context.session_state["remembered"] = value
        return "remembered"

    def recall(run_context: RunContext) -> str:
        return f"recalled:{run_context.session_state.get('remembered')}"

    original = agents.get_tool_by_name

    def build(name: str, *args: object, **kwargs: object) -> object:
        if name != "calculator":
            return original(name, *args, **kwargs)
        return Toolkit(name="calculator", tools=[remember, recall])

    monkeypatch.setattr(agents, "get_tool_by_name", build)

    async def child_response(child: DelegationChild, **_kwargs: object) -> str:
        child_agent = Agent(
            id="code",
            db=create_session_storage("code", runtime.config, runtime.runtime_paths, child_execution_identity(child)),
            model=DelegationModel(id="test", responses=[ModelResponse(content="child done")]),
        )
        try:
            await child_agent.arun(
                child.task,
                run_id=child.run_id,
                session_id=child.session_id,
                user_id=runtime.requester_id,
            )
        finally:
            child_agent.db.close()
        return "child done"

    monkeypatch.setattr(ai, "run_delegated_child_response", child_response)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    command = _call_and_wait("00000000-0000-4000-8000-000000000005", "calculator", "remember", {"value": 7})
    provider.steps = [
        [("run_subagent", {"agent_name": "code", "task": "work"})],
        [("run_shell_command", {"args": command})],
        [("recall", {})],
        "done",
    ]

    async with asyncio.timeout(60):
        assert "done" in await _respond(runtime)

    tool_outputs = [
        str(message["content"]) for message in provider.requests[3]["messages"] if message["role"] == "tool"
    ]
    assert '"status":"completed"' in tool_outputs[-2].replace(" ", ""), tool_outputs[-2]
    assert tool_outputs[-1] == "recalled:7", tool_outputs


@pytest.mark.asyncio
async def test_a_timer_cancelled_during_a_reply_does_not_keep_its_agent_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """A cancelled timer keeps the context it was scheduled from until its deadline, which must not hold the Agent."""
    runtime = _helper_runtime(tmp_path, running_api)
    timers: list[asyncio.TimerHandle] = []
    replying_agents: list[weakref.ref[Agent]] = []

    async def wait(agent: Agent) -> str:
        replying_agents.append(weakref.ref(agent))
        # Like a request timeout that was not needed: cancelled, yet scheduled until its deadline.
        timer = asyncio.get_running_loop().call_later(3600, lambda: None)
        timer.cancel()
        timers.append(timer)
        return "waited"

    original = agents.get_tool_by_name

    def build(name: str, *args: object, **kwargs: object) -> object:
        if name != "calculator":
            return original(name, *args, **kwargs)
        return Toolkit(name="calculator", tools=[wait])

    monkeypatch.setattr(agents, "get_tool_by_name", build)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [[("wait", {})], "done"]

    assert "done" in await _respond(runtime)
    gc.collect()

    assert _tool_output(provider) == "waited"
    assert timers[0].cancelled()
    assert replying_agents[0]() is None
