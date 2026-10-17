"""Full context remains available without bloating the actual provider prompt."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import _messages
from agno.learn import LearningMachine
from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput
from agno.session import AgentSession
from agno.session.summary import SessionSummary

from mindroom import agents
from mindroom.config.agent import AgentConfig
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.tool_system.agent_tool_calls import execute_agent_tool_call
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from tests.test_agent_cli_authority import _runtime_context

if TYPE_CHECKING:
    from pathlib import Path

    from agno.learn.stores.protocol import LearningStore


@pytest.mark.asyncio
async def test_context_uses_same_agent_learning_and_summary_and_restores_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal SDK context reads remain available; only bootstrap reaches the provider."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        memory_backend="file",
        learning=False,
    )
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        persist_runtime_state=False,
    )
    agent.system_message = "small bootstrap"
    agent.add_session_summary_to_context = True
    agent.context_documents = {}
    agent._learning = LearningMachine()
    seen = []

    async def context(_self: LearningMachine, **kwargs: object) -> str:
        seen.append(kwargs)
        return "private learned context"

    monkeypatch.setattr(LearningMachine, "abuild_context", context)
    session = AgentSession(
        session_id="session",
        user_id=runtime.requester_id,
        summary=SessionSummary(summary="complete previous summary"),
    )
    run = RunContext(run_id="run", session_id="session", user_id=runtime.requester_id, session_state={})
    output = RunOutput(run_id="run", input=RunInput(input_content="current user message"))
    with tool_runtime_context(runtime):
        catalog = await agent.prepare_execution_catalog(output, run, session, user_id=runtime.requester_id)
        await catalog.close()
    result = await _messages.aget_run_messages(
        agent,
        run_response=output,
        session=session,
        run_context=run,
        input="current user message",
    )
    assert result.system_message.content == "small bootstrap"
    assert "private learned context" in agent.context_documents["agent-context"]
    assert "complete previous summary" in agent.context_documents["agent-context"]
    assert seen[0]["user_id"] == runtime.requester_id
    assert seen[0]["message"] == "current user message"

    async def failure(_self: LearningMachine, **_kwargs: object) -> str:
        msg = "retrieval failed"
        raise OSError(msg)

    monkeypatch.setattr(LearningMachine, "abuild_context", failure)
    with (
        tool_runtime_context(runtime),
        pytest.raises(RuntimeError, match=r"retrieval failed.*!mode helper standard") as error,
    ):
        await agent.prepare_execution_catalog(output, run, session, user_id=runtime.requester_id)
    assert isinstance(error.value.__cause__, OSError)
    assert agent.system_message == "small bootstrap"


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["@alice:example.test", "@bob:example.test"])
async def test_hidden_generated_learning_keeps_authenticated_requester(tmp_path: Path, requester: str) -> None:
    """Generated closures receive the actual requester explicitly for every minimal catalog."""
    runtime = replace(_runtime_context(tmp_path), requester_id=requester)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        memory_backend="file",
        learning=False,
    )
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        session_id=runtime.session_id,
    )

    async def tools(user_id: str, session_id: str, agent_id: str, **_kwargs: object) -> list:
        async def remember(value: str) -> str:
            return f"{agent_id}/{user_id}/{session_id}: {value}"

        return [remember]

    store = cast("LearningStore", SimpleNamespace(aget_tools=tools))
    agent._learning = LearningMachine(custom_stores={"local": store})
    with ExitStack() as resources, tool_runtime_context(runtime):
        resources.callback(close_agent_runtime_state_dbs, agent)
        catalog = await agent.prepare_execution_catalog(
            RunOutput(run_id="run"),
            RunContext(run_id="run", session_id=runtime.session_id, user_id=requester, session_state={}),
            AgentSession(session_id=runtime.session_id),
            user_id=requester,
        )
        try:
            binding = await catalog.bind(ToolKey("agent", "remember"))
            events = [event async for event in execute_agent_tool_call(binding, "inner", {"value": "lesson"})]
            assert events[-1].execution.result == f"helper/{requester}/{runtime.session_id}: lesson"
        finally:
            await catalog.close()
