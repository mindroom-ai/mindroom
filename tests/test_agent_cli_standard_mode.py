"""Standard-mode agents call their other tools from inside their own shell commands."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom import agents, ai
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.cli_shell_agent import STANDARD_CLI_NOTE, CliShellAgent
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context
from tests.test_agent_cli_shell_access import running_api  # noqa: F401 - pytest fixture

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _helper_runtime(tmp_path: Path, registry: TurnToolRegistry, **agent: object) -> ToolRuntimeContext:
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "calculator"],
        memory_backend="file",
        learning=False,
        **agent,
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
async def test_standard_shell_command_calls_another_tool_through_mindroom_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """A native shell command composes a native tool call; the grant ends with the response."""
    runtime = _helper_runtime(tmp_path, running_api)
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


@pytest.mark.asyncio
async def test_approval_gated_tools_stay_native_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,  # noqa: F811 - pytest fixture
) -> None:
    """A tool that may need approval keeps its native confirmation and is not offered to the CLI."""
    runtime = _helper_runtime(tmp_path, running_api)
    runtime.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="add", action="require_approval")]
    )
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [[("run_shell_command", {"args": "mindroom-agent tools list"})], "done"]

    assert "done" in await _respond(runtime)

    assert "add" in {tool["function"]["name"] for tool in provider.requests[0]["tools"]}
    listing = _tool_output(provider).replace(" ", "")
    assert '"function":"subtract"' in listing, listing
    assert '"function":"add"' not in listing, listing


@pytest.mark.parametrize(("channel", "api_running"), [("matrix", False), ("openai_compat", True), ("matrix", True)])
def test_cli_shell_agent_needs_a_matrix_turn_and_the_running_api(
    tmp_path: Path,
    channel: str,
    *,
    api_running: bool,
) -> None:
    """Without the API server, or outside Matrix, the agent gets neither the CLI class nor its prompt note."""
    runtime = _helper_runtime(tmp_path, registry=SimpleNamespace())
    identity = replace(build_execution_identity_from_runtime_context(runtime), channel=channel)
    if api_running:
        set_api_server_address("127.0.0.1", 8765)
    try:
        agent = agents.create_agent("helper", runtime.config, runtime.runtime_paths, identity)
    finally:
        clear_api_server_address()

    try:
        eligible = channel == "matrix" and api_running
        assert isinstance(agent, CliShellAgent) is eligible
        assert isinstance(agent, KnowledgeToolDescribingAgent)
        assert (STANDARD_CLI_NOTE in agent.instructions) is eligible
    finally:
        close_agent_runtime_state_dbs(agent)
