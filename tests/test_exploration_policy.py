"""Exploration turns call only the operator's allowlist, never pause for approval, and stop at their call cap."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from agno.knowledge.knowledge import Knowledge
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session import AgentSession
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents
from mindroom.automations.exploration_policy import (
    ExplorationPolicy,
    claim_exploration_call,
    exploration_policy_for_turn,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.models import ModelConfig
from mindroom.hooks import HookRegistry
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.test_agent_cli_authority import _runtime_context
from tests.test_mcp_toolkit import _oauth_server_config, _OAuthRequiredManager, _runtime_paths, _worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

POLICY = ExplorationPolicy(tools=("file.read_file", "knowledge_graph"), max_tool_calls=5)


def _runtime(tmp_path: Path) -> ToolRuntimeContext:
    runtime = _runtime_context(tmp_path)
    runtime.config.models["luna"] = ModelConfig(provider="openai", id="gpt-6-luna")
    runtime.config.knowledge_graph.model = "luna"
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["file", "shell"],
        knowledge_graph=True,
        automations=[{"name": "exploration", "tools": ["file.read_file"], "max_tool_calls": 5}],
    )
    return runtime


async def _function_names(agent: Agent) -> list[str]:
    run_context = RunContext(run_id="run-1", session_id="session-1", user_id="@alice:example.test")
    tools = await agent.aget_tools(RunOutput(run_id="run-1"), run_context, AgentSession(session_id="session-1"))
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, Toolkit):
            names.extend(tool.get_async_functions())
        elif isinstance(tool, Function):
            names.append(tool.name)
    return sorted(names)


def _agent(runtime: ToolRuntimeContext, exploration: ExplorationPolicy | None, **options: object) -> Agent:
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        supports_native_tool_approval=True,
        exploration=exploration,
        **options,
    )


def _write_skill(runtime: ToolRuntimeContext, name: str) -> None:
    skill = agent_workspace_root_path(runtime.runtime_paths.storage_root, "helper") / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: The {name} skill\n---\n\n{name} body\n")


def test_policy_applies_only_to_the_exploration_prompt(tmp_path: Path) -> None:
    """The exploration prompt's turn is restricted; a person's reply and other automations are not."""
    config = _runtime(tmp_path).config

    assert exploration_policy_for_turn(config, "helper", "automation/exploration") == ExplorationPolicy(
        tools=("file.read_file", "knowledge_graph"),
        max_tool_calls=5,
    )
    assert exploration_policy_for_turn(config, "helper", None) is None
    assert exploration_policy_for_turn(config, "helper", "automation/dreaming") is None


@pytest.mark.asyncio
async def test_exploration_build_keeps_only_allowlisted_functions(tmp_path: Path) -> None:
    """Only the listed function and the graph lookups are offered; other toolkits are not built."""
    names = await _function_names(_agent(_runtime(tmp_path), POLICY))

    assert names == ["graph_entity", "read_file", "search_graph"]


@pytest.mark.asyncio
async def test_exploration_build_hides_approval_gated_functions(tmp_path: Path) -> None:
    """A function that needs approval is absent, since nobody is there to approve it."""
    runtime = _runtime(tmp_path)
    runtime.config.tool_approval.rules.append(ApprovalRuleConfig(match="read_file", action="require_approval"))

    assert "read_file" not in await _function_names(_agent(runtime, POLICY))
    assert "read_file" in await _function_names(_agent(runtime, None))


@pytest.mark.asyncio
async def test_ordinary_turn_of_the_same_agent_is_unrestricted(tmp_path: Path) -> None:
    """Outside exploration the agent keeps all of its tools."""
    names = await _function_names(_agent(_runtime(tmp_path), None))

    assert "run_shell_command" in names
    assert "save_file" in names


def test_tool_call_limit_is_the_smaller_cap(tmp_path: Path) -> None:
    """An exploration turn stops at its own cap even when the agent allows more per turn."""
    runtime = _runtime(tmp_path)

    assert _agent(runtime, POLICY).tool_call_limit == 5
    assert _agent(runtime, None).tool_call_limit == runtime.config.resolve_entity("helper").max_tool_calls_per_turn


def test_thread_cap_counts_calls_per_thread() -> None:
    """Each exploration thread gets its own budget."""
    assert [claim_exploration_call("helper", "$cap-a", 2) for _ in range(3)] == [True, True, False]
    assert claim_exploration_call("helper", "$cap-b", 2)


@pytest.mark.asyncio
async def test_bridge_declines_calls_past_the_thread_cap(tmp_path: Path) -> None:
    """Calls past the cap are declined however many times the turn continues."""
    runtime = replace(_runtime(tmp_path), agent_name="helper", hook_source="automation/exploration")
    bridge = build_tool_hook_bridge(HookRegistry.empty(), agent_name="helper")
    tool = AsyncMock(return_value="ok")

    with tool_runtime_context(runtime):
        results = [await bridge("read_file", tool, {"path": "a.txt"}) for _ in range(6)]

    assert results[:5] == ["ok"] * 5
    assert "exploration run may make at most 5 tool calls" in str(results[5])
    assert tool.await_count == 5


@pytest.mark.asyncio
async def test_exploration_build_hides_skill_and_knowledge_functions(tmp_path: Path) -> None:
    """Skill scripts and knowledge search are not on the allowlist, so exploration cannot call them."""
    runtime = _runtime(tmp_path)
    runtime.config.agents["helper"].memory_backend = "file"
    _write_skill(runtime, "deploy")

    names = await _function_names(_agent(runtime, POLICY, knowledge=Knowledge(name="docs")))

    assert names == ["graph_entity", "read_file", "search_graph"]


@pytest.mark.asyncio
async def test_exploration_build_runs_every_call_inline(tmp_path: Path) -> None:
    """Background jobs would finish in turns without the exploration restrictions, so none start."""
    runtime = _runtime(tmp_path)
    runtime.config.background_tool_jobs.enabled = True

    agent = _agent(runtime, POLICY)

    assert "job" not in await _function_names(agent)
    assert not vars(agent.model).get("_mindroom_tool_jobs")
    assert vars(_agent(runtime, None).model).get("_mindroom_tool_jobs")


def test_exploration_drops_the_mcp_dispatcher(tmp_path: Path) -> None:
    """An OAuth MCP server's call_tool can reach any remote tool, including ones approval rules hide."""
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=_OAuthRequiredManager(),
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        worker_target=_worker_target(),
    )

    agents._drop_exploration_dispatchers(toolkit)

    assert "demo_call_tool" not in toolkit.async_functions
    assert "demo_list_tools" in toolkit.async_functions


@pytest.mark.asyncio
async def test_exploration_runs_with_the_tools_that_built(tmp_path: Path) -> None:
    """An allowlisted toolkit that cannot load leaves exploration with the rest instead of failing the run."""
    runtime = _runtime(tmp_path)
    policy = ExplorationPolicy(tools=("file.read_file", "shell", "knowledge_graph"), max_tool_calls=5)
    runtime.config.agents["helper"].tools = ["file"]

    assert await _function_names(_agent(runtime, policy)) == ["graph_entity", "read_file", "search_graph"]


def test_minimal_exploration_lists_only_the_toolkits_it_can_call(tmp_path: Path) -> None:
    """A minimal-mode exploration agent is told only about its allowlisted toolkits."""
    agent = _agent(_runtime(tmp_path), POLICY, agent_mode="minimal")

    [roster] = [line for line in str(agent.system_message).splitlines() if line.startswith("Toolkits callable")]
    assert "file" in roster
    assert "shell" not in roster
