"""Portable deferred tool search: fixed wire tools, call_tool rewrite, and history projection."""
# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput

from mindroom.providers.portable_tool_search import (
    install_portable_deferred_tool_search,
    portable_deferred_tool_names,
    project_portable_request,
    resolve_call_tool_calls,
)
from mindroom.providers.synthetic_model import SyntheticModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DEFERRED = frozenset({"crm_lookup"})


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}


def _name(tool: dict[str, Any]) -> str:
    return tool["function"]["name"]


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_projection_strips_deferred_schemas_and_keeps_order() -> None:
    tools = [_tool("call_tool"), _tool("crm_lookup"), _tool("shell"), _tool("tool_search")]

    _, wire_tools = project_portable_request([], tools, DEFERRED)

    assert wire_tools is not None
    assert [_name(tool) for tool in wire_tools] == ["call_tool", "shell", "tool_search"]


def test_projection_wraps_deferred_history_without_mutating_it() -> None:
    deferred_call = _call("c1", "crm_lookup", '{"id": 7}')
    eager_call = _call("c2", "shell", "{}")
    assistant = Message(role="assistant", tool_calls=[deferred_call, eager_call])
    result = Message(role="tool", tool_call_id="c1", tool_name="crm_lookup", content="ok")
    user = Message(role="user", content="hi")

    projected, _ = project_portable_request([user, assistant, result], None, DEFERRED)

    assert projected[0] is user
    wrapped = projected[1].tool_calls[0]
    assert wrapped["id"] == "c1"
    assert wrapped["function"]["name"] == "call_tool"
    assert json.loads(wrapped["function"]["arguments"]) == {"name": "crm_lookup", "arguments": {"id": 7}}
    assert projected[1].tool_calls[1] is assistant.tool_calls[1]
    assert projected[2].tool_name == "call_tool"
    assert projected[2].tool_call_id == "c1"
    assert assistant.tool_calls[0]["function"]["name"] == "crm_lookup"
    assert result.tool_name == "crm_lookup"


def test_projection_is_byte_stable() -> None:
    assistant = Message(role="assistant", tool_calls=[_call("c1", "crm_lookup", '{"id": 7, "note": "é"}')])

    first, _ = project_portable_request([assistant], None, DEFERRED)
    second, _ = project_portable_request([assistant], None, DEFERRED)

    assert first[0].tool_calls == second[0].tool_calls


def test_projection_leaves_unknown_legacy_calls() -> None:
    legacy_call = _call("c1", "load_tool", '{"tool_name": "crm"}')
    assistant = Message(role="assistant", tool_calls=[legacy_call])

    projected, _ = project_portable_request([assistant], None, DEFERRED)

    assert projected[0] is assistant


def test_resolve_call_tool_to_real_function_preserving_extra_keys() -> None:
    call = {
        **_call("c1", "call_tool", '{"name": "crm_lookup", "arguments": {"id": 7}}'),
        "thought_signature": "sig",
    }

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup", "call_tool"})

    assert resolved["function"] == {"name": "crm_lookup", "arguments": '{"id": 7}'}
    assert resolved["id"] == "c1"
    assert resolved["thought_signature"] == "sig"


def test_resolve_accepts_json_string_arguments() -> None:
    call = _call("c1", "call_tool", json.dumps({"name": "crm_lookup", "arguments": '{"id": 7}'}))

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup"})

    assert json.loads(resolved["function"]["arguments"]) == {"id": 7}


def test_resolve_missing_arguments_means_empty_object() -> None:
    call = _call("c1", "call_tool", '{"name": "crm_lookup"}')

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup"})

    assert resolved["function"] == {"name": "crm_lookup", "arguments": "{}"}


@pytest.mark.parametrize(
    "arguments",
    [
        '{"name": "nope"}',
        '{"name": "crm_lookup", "arguments": [1]}',
        '{"name": "crm_lookup", "arguments": "not json"}',
        "not json",
        '{"name": "call_tool"}',
        '{"arguments": {}}',
    ],
)
def test_resolve_leaves_unresolvable_calls(arguments: str) -> None:
    call = _call("c1", "call_tool", arguments)

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup", "call_tool"})

    assert resolved is call


def test_resolve_mixed_batch_rewrites_only_the_wrapper() -> None:
    eager_call = _call("c2", "shell", '{"command": "ls"}')
    calls = [_call("c1", "call_tool", '{"name": "crm_lookup", "arguments": {"id": 7}}'), eager_call]

    resolved = resolve_call_tool_calls(calls, {"crm_lookup", "shell"})

    assert [call["function"]["name"] for call in resolved] == ["crm_lookup", "shell"]
    assert resolved[1] is eager_call


class _ScriptedModel(SyntheticModel):
    """Answer each provider request from a script and record what the request carried."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.responses = responses
        self.requests: list[tuple[list[Message], list[dict[str, Any]] | None]] = []

    async def ainvoke(self, messages: list[Message], **kwargs: Any) -> ModelResponse:  # noqa: ANN401
        self.requests.append((list(messages), kwargs.get("tools")))
        return self.responses[len(self.requests) - 1]

    async def ainvoke_stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[ModelResponse]:  # noqa: ANN401
        yield await self.ainvoke(messages, **kwargs)


async def _run(agent: Agent, *, stream: bool) -> RunOutput:
    if not stream:
        output = await agent.arun("Look up customers 7 and 8")
        assert isinstance(output, RunOutput)
        return output
    final: RunOutput | None = None
    async for event in agent.arun("Look up customers 7 and 8", stream=True, yield_run_output=True):
        if isinstance(event, RunOutput):
            final = event
    assert final is not None
    return final


def _call_names(messages: list[Message]) -> list[str]:
    return [call["function"]["name"] for message in messages if message.tool_calls for call in message.tool_calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_installed_model_routes_call_tool_and_keeps_real_names_in_history(*, stream: bool) -> None:
    looked_up: list[int] = []

    def crm_lookup(customer_id: int) -> str:
        looked_up.append(customer_id)
        return f"customer {customer_id}"

    def call_tool(name: str, arguments: dict[str, Any] | None = None) -> str:
        return f"fallback {name} {arguments}"

    model = _ScriptedModel(
        [
            ModelResponse(
                tool_calls=[_call("c1", "call_tool", '{"name": "crm_lookup", "arguments": {"customer_id": 7}}')],
            ),
            # An unconstrained server may let the model call a discovered function by its real name.
            ModelResponse(tool_calls=[_call("c2", "crm_lookup", '{"customer_id": 8}')]),
            ModelResponse(content="done"),
        ],
    )
    install_portable_deferred_tool_search(model, deferred_tool_names=frozenset({"crm_lookup"}))
    agent = Agent(name="Assistant", model=model, tools=[crm_lookup, call_tool], telemetry=False)

    output = await _run(agent, stream=stream)

    assert output.content == "done"
    assert looked_up == [7, 8]
    assert portable_deferred_tool_names(model) == {"crm_lookup"}
    for _messages, tools in model.requests:
        assert sorted(_name(tool) for tool in tools or []) == ["call_tool"]
    assert _call_names(model.requests[1][0]) == ["call_tool"]
    assert _call_names(model.requests[2][0]) == ["call_tool", "call_tool"]
    assert {message.tool_name for message in model.requests[2][0] if message.role == "tool"} == {"call_tool"}
    assert _call_names(output.messages or []) == ["crm_lookup", "crm_lookup"]
