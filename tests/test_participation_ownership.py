"""Participation belongs to the primary response, not concurrent model helpers."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.compression.manager import CompressionManager
from agno.db.in_memory import InMemoryDb
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.utils.models.claude import format_messages
from anthropic.types import Message as AnthropicMessage

from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.claude_prompt_cache import (
    install_claude_deferred_tool_search,
    install_claude_prompt_cache_hook,
    prepare_claude_request_kwargs,
)
from mindroom.hooks.enrichment import render_transient_context
from mindroom.participation import ParticipationGate, participation_model
from mindroom.synthetic_model import SyntheticModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("helper", ["compression", "learning"])
@pytest.mark.parametrize("approved", [False, True])
async def test_helpers_cannot_acquire_primary_decision(helper: str, streaming: bool, approved: bool) -> None:
    """Real compression and default learning must keep their own model requests."""
    requests: list[tuple[bool, list[Message], object]] = []

    class Model(SyntheticModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            deciding = "Decide whether to participate" in str(messages[-1].content)
            requests.append((deciding, deepcopy(messages), deepcopy(kwargs.get("tools"))))
            # Yield so Agno's parallel compression/learning requests overlap the check.
            await asyncio.sleep(0)
            if deciding:
                action = "respond" if approved else "stay_silent"
                return ModelResponse(content='{"action":"' + action + '","reason":"Conversation context."}')
            return ModelResponse(content="Useful answer")

        async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
            yield await self.ainvoke(messages, **kwargs)

    model = Model(id="test", name="test", provider="test")
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        if helper == "compression":
            manager = CompressionManager(model=model, compress_tool_results_limit=1)
            messages = [
                Message(role="system", content="Primary conversation system"),
                Message(role="tool", content="First result", tool_name="lookup", tool_call_id="one"),
                Message(role="tool", content="Second result", tool_name="lookup", tool_call_id="two"),
                Message(role="user", content="What do these results mean?"),
            ]
            tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
            run = RunOutput(run_id="primary")
            if streaming:
                chunks = [
                    chunk
                    async for chunk in model.aresponse_stream(
                        messages,
                        tools=tools,
                        compression_manager=manager,
                        run_response=run,
                    )
                ]
                assert (
                    any(chunk.content == "Useful answer" for chunk in chunks if isinstance(chunk, ModelResponse))
                    is approved
                )
            else:
                response = await model.aresponse(messages, tools=tools, compression_manager=manager, run_response=run)
                assert bool(response.content) is approved
        else:
            agent = Agent(model=model, db=InMemoryDb(), learning=True, telemetry=False)
            if streaming:
                chunks = [
                    chunk
                    async for chunk in agent.arun(
                        "My name is Alex. Can you help?",
                        run_id="primary",
                        user_id="alex",
                        stream=True,
                    )
                ]
                assert any(chunk.content == "Useful answer" for chunk in chunks) is approved
            else:
                response = await agent.arun("My name is Alex. Can you help?", run_id="primary", user_id="alex")
                assert bool(response.content) is approved

    decisions = [request for request in requests if request[0]]
    assert len(decisions) == 1
    decision = decisions[0]
    replies = [request for request in requests if not request[0] and request[1] == decision[1][:-1]]
    assert len(replies) == int(approved)
    if approved:
        assert decision[2] == replies[0][2]
    if helper == "compression":
        assert decision[1][0].content == "Primary conversation system"
    else:
        assert "My name is Alex. Can you help?" in str(decision[1][-2].content)
    # Helpers really ran; replacing compression or disabling learning would hide the defect.
    assert len(requests) >= 3 + int(approved)
    assert gate.approved is approved


@pytest.mark.asyncio
async def test_simultaneous_primary_calls_share_one_decision() -> None:
    """Concurrent entries must not issue competing participation decisions."""
    entered = asyncio.Event()
    release = asyncio.Event()
    decisions = 0

    async def invoke(**_kwargs: object) -> ModelResponse:
        nonlocal decisions
        decisions += 1
        entered.set()
        await release.wait()
        return ModelResponse(content='{"action":"respond","reason":"Open question."}')

    gate = ParticipationGate()
    model = SyntheticModel(id="test", name="test", provider="test")
    messages = [Message(role="user", content="Question")]
    first = asyncio.create_task(gate.check(model, invoke, messages, {}))
    await entered.wait()
    second = asyncio.create_task(gate.check(model, invoke, messages, {}))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert decisions == 1


@pytest.mark.asyncio
async def test_late_approval_cannot_reopen_failed_turn() -> None:
    """An in-flight provider answer must not reopen activity after quiet failure."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def invoke(**_kwargs: object) -> ModelResponse:
        entered.set()
        await release.wait()
        return ModelResponse(content='{"action":"respond","reason":"Open question."}')

    gate = ParticipationGate()
    model = SyntheticModel(id="test", name="test", provider="test")
    decision = asyncio.create_task(gate.check(model, invoke, [Message(role="user", content="Question")], {}))
    await entered.wait()
    gate.decline("preparation_failed")
    assert gate.decided.is_set()
    release.set()

    assert not await decision
    assert gate.is_silent
    assert gate.decision is not None
    assert gate.decision.reason == "preparation_failed"


@pytest.mark.asyncio
async def test_settled_approval_cannot_be_overwritten() -> None:
    """Late errors must not revoke a turn that already owns visible output."""

    async def invoke(**_kwargs: object) -> ModelResponse:
        return ModelResponse(content='{"action":"respond","reason":"Open question."}')

    gate = ParticipationGate()
    model = SyntheticModel(id="test", name="test", provider="test")
    assert await gate.check(model, invoke, [Message(role="user", content="Question")], {})
    gate.decline("late_failure")
    assert gate.approved


@pytest.mark.asyncio
async def test_claude_wire_payload_preserves_prefix_with_transient_context() -> None:
    """Provider formatting must leave the decision suffix after the entire reply prefix."""
    requests: list[dict[str, Any]] = []

    class Model(MindRoomAnthropicClaude):
        async def ainvoke(self, messages: list[Message], *_args: object, **kwargs: object) -> ModelResponse:
            formatted, system = format_messages(messages)
            requests.append(
                prepare_claude_request_kwargs(
                    self,
                    {
                        "messages": formatted,
                        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                        "tools": kwargs.get("tools"),
                    },
                ),
            )
            if "Decide whether to participate" in str(messages[-1].content):
                return ModelResponse(content='{"action":"respond","reason":"Open question."}')
            return ModelResponse(content="Useful answer")

    model = Model(id="test", cache_system_prompt=True)
    messages = [
        Message(role="system", content="System"),
        Message(role="user", content="Open question"),
        Message(role="user", content=render_transient_context(["Current Matrix target"])),
    ]
    with participation_model(model, ParticipationGate(), run_id="primary"):
        await model.aresponse(messages, run_response=RunOutput(run_id="primary"))
    decision, reply = requests
    suffix = decision["messages"][-1]["content"].pop()
    assert "Decide whether to participate" in suffix["text"]
    assert decision == reply


@pytest.mark.asyncio
@pytest.mark.parametrize("native_source", ["tools", "skills", "deferred_search", "extra_body", "mcp_servers"])
async def test_claude_native_tools_cannot_execute_during_decision(
    monkeypatch: pytest.MonkeyPatch,
    native_source: str,
) -> None:
    """The final SDK payload must disable native tools, including provider-injected ones."""
    requests: list[dict[str, Any]] = []
    unapproved_executions: list[str] = []

    class Messages:
        async def create(self, **kwargs: Any) -> AnthropicMessage:  # noqa: ANN401
            requests.append(deepcopy(kwargs))
            deciding = "Decide whether to participate" in json.dumps(kwargs["messages"])
            effective = {**kwargs, **kwargs.get("extra_body", {})}
            if deciding and effective.get("tool_choice") != {"type": "none"}:
                unapproved_executions.append(native_source)
            return AnthropicMessage.model_validate(
                {
                    "id": "test-response",
                    "type": "message",
                    "role": "assistant",
                    "model": "test",
                    "content": [
                        {
                            "type": "text",
                            "text": '{"action":"respond","reason":"Open question."}' if deciding else "Useful answer",
                        },
                    ],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )

    messages_api = Messages()
    client = SimpleNamespace(messages=messages_api, beta=SimpleNamespace(messages=messages_api))
    model = MindRoomAnthropicClaude(
        id="test",
        cache_system_prompt=True,
        request_params={"tool_choice": {"type": "auto"}},
    )
    monkeypatch.setattr(model, "get_async_client", lambda: client)
    install_claude_prompt_cache_hook(model)
    tools: list[dict[str, Any]] = []
    if native_source == "tools":
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    elif native_source == "skills":
        model.skills = [{"type": "anthropic", "skill_id": "xlsx", "version": "latest"}]
    elif native_source == "extra_body":
        model.request_params["extra_body"] = {
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "auto"},
        }
    elif native_source == "mcp_servers":
        model.request_params["mcp_servers"] = [{"type": "url", "url": "https://example.com/mcp", "name": "remote"}]
    else:
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"lookup"}))

    authored_params = deepcopy(model.request_params)
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        response = await model.aresponse(
            [Message(role="user", content="Open question")],
            tools=tools,
            run_response=RunOutput(run_id="primary"),
        )
    assert gate.approved
    assert response.content == "Useful answer"
    assert unapproved_executions == []
    assert requests[0].get("tools") == requests[1].get("tools")
    assert requests[1]["tool_choice"] == {"type": "auto"}
    assert model.request_params == authored_params
