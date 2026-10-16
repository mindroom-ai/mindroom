"""Exploration turns call only the operator's allowlist, never pause for approval, and stop at their call cap."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
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
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge
from tests.test_agent_cli_authority import _runtime_context

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


def _agent(runtime: ToolRuntimeContext, exploration: ExplorationPolicy | None) -> Agent:
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        supports_native_tool_approval=True,
        exploration=exploration,
    )


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
