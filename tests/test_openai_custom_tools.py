"""Tests for freeform custom tool calls on the OpenAI Responses API and the Codex backend."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.message import Message
from openai import AsyncOpenAI

from mindroom.agents import _set_toolkit_approval_origin
from mindroom.agno_compat_tool_dialect import install_tool_dialect
from mindroom.codex_model import CodexResponses
from mindroom.custom_tools.coding import CodingTools
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.openai_tool_search import request_params_with_deferred_tool_search
from mindroom.tool_dialect_codex import CODEX_DIALECT

if TYPE_CHECKING:
    from pathlib import Path

_PATCH = "*** Begin Patch\n*** Add File: hello.txt\n+hi\n*** End Patch"
_CUSTOM_CALL = {
    "type": "custom_tool_call",
    "id": "ctc_patch",
    "call_id": "call_patch",
    "name": "apply_patch",
    "input": _PATCH,
    "status": "completed",
}
_REASONING = {"type": "reasoning", "id": "rs_patch", "summary": [], "encrypted_content": "opaque"}
_ANSWER = {
    "type": "message",
    "id": "msg_answer",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Done", "annotations": []}],
}


def _response(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"resp_{len(output)}",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-astra",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 1,
            "total_tokens": 11,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "error": None,
        "incomplete_details": None,
    }


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': 0, **fields})}\n\n"


def _stream(output: list[dict[str, Any]]) -> str:
    events = _event("response.created", response={**_response([]), "status": "in_progress"})
    for index, item in enumerate(output):
        if item["type"] == "custom_tool_call":
            events += _event("response.output_item.added", output_index=index, item={**item, "input": ""})
            events += _event(
                "response.custom_tool_call_input.delta",
                output_index=index,
                item_id=item["id"],
                delta=item["input"],
            )
        else:
            events += _event("response.output_item.added", output_index=index, item=item)
        events += _event("response.output_item.done", output_index=index, item=item)
    return events + _event("response.completed", response=_response(output))


class _Provider:
    def __init__(self, first_output: list[dict[str, Any]]) -> None:
        self.outputs = [first_output]
        self.requests: list[dict[str, Any]] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        output = self.outputs.pop(0) if self.outputs else [_ANSWER]
        if payload.get("stream"):
            return httpx.Response(200, text=_stream(output), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_response(output))


async def _run(
    provider: _Provider,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
    codex: bool,
) -> None:
    coding = CodingTools(base_dir=str(workspace))
    _set_toolkit_approval_origin(coding, "coding")
    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
    ) as client:
        if codex:
            monkeypatch.setattr(CodexResponses, "get_async_client", lambda _self: client)
            model = CodexResponses(id="gpt-6.1-sol")
        else:
            model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=False)
        install_tool_dialect(model, CODEX_DIALECT)
        agent = Agent(model=model, tools=[coding], telemetry=False)
        if stream:
            async for _ in agent.arun("Create hello.txt.", stream=True):
                pass
        else:
            await agent.arun("Create hello.txt.")


@pytest.mark.parametrize(("stream", "codex"), [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.asyncio
async def test_end_to_end_apply_patch_edits_workspace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
    codex: bool,
) -> None:
    """A freeform apply_patch call edits the workspace and replays as custom items."""
    provider = _Provider([_CUSTOM_CALL])

    await _run(provider, tmp_path, monkeypatch, stream=stream, codex=codex)

    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    [tool] = [tool for tool in provider.requests[0]["tools"] if tool.get("name") == "apply_patch"]
    assert tool["type"] == "custom"
    assert tool["format"]["syntax"] == "lark"
    replay = provider.requests[1]["input"]
    [call] = [item for item in replay if item.get("type") == "custom_tool_call"]
    assert (call["call_id"], call["name"], call["input"]) == ("call_patch", "apply_patch", _PATCH)
    [output] = [item for item in replay if item.get("type") == "custom_tool_call_output"]
    assert output == {
        "type": "custom_tool_call_output",
        "call_id": "call_patch",
        "output": "Success. Updated the following files:\nA hello.txt",
    }
    assert not [item for item in replay if item.get("type") in {"function_call", "function_call_output"}]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_reasoning_order_survives_custom_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    """Reasoning that preceded a custom call replays before it, then the call's output."""
    provider = _Provider([_REASONING, _CUSTOM_CALL])

    await _run(provider, tmp_path, monkeypatch, stream=stream, codex=False)

    kinds = [item.get("type") for item in provider.requests[1]["input"] if item.get("type")]
    assert kinds[-3:] == ["reasoning", "custom_tool_call", "custom_tool_call_output"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_stored_continuation_sends_custom_output(tmp_path: Path, *, stream: bool) -> None:
    """A stored-response continuation that omits the call still answers it with a custom tool output."""
    provider = _Provider([_CUSTOM_CALL])
    coding = CodingTools(base_dir=str(tmp_path))
    _set_toolkit_approval_origin(coding, "coding")
    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
    ) as client:
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=True)
        install_tool_dialect(model, CODEX_DIALECT)
        agent = Agent(model=model, tools=[coding], telemetry=False)
        if stream:
            async for _ in agent.arun("Create hello.txt.", stream=True):
                pass
        else:
            await agent.arun("Create hello.txt.")

    second = provider.requests[1]
    outputs = [item for item in second["input"] if str(item.get("type", "")).endswith("_output")]
    assert outputs == [
        {
            "type": "custom_tool_call_output",
            "call_id": "call_patch",
            "output": "Success. Updated the following files:\nA hello.txt",
        },
    ]


def test_deferred_custom_tool_is_deferred() -> None:
    """A deferred toolkit's freeform apply_patch loads on demand like its function tools."""
    custom = {"type": "custom", "name": "apply_patch", "description": "patch", "format": {"type": "grammar"}}
    function = {"type": "function", "name": "read_file", "description": "read", "parameters": {"type": "object"}}

    prepared = request_params_with_deferred_tool_search(
        {"tools": [custom, function]},
        frozenset({"apply_patch", "read_file"}),
    )

    assert prepared["tools"] == [
        {"type": "tool_search"},
        {**custom, "defer_loading": True},
        {**function, "defer_loading": True},
    ]


@pytest.mark.asyncio
async def test_codex_non_stream_invocation_projects_history_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Codex model streams under a non-stream call; history renders once, so same-named calls keep their IDs."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=_stream([_ANSWER]), headers={"content-type": "text/event-stream"})

    kill_call = {
        "id": "fc_kill",
        "call_id": "call_kill",
        "type": "function",
        "function": {
            "name": "kill_shell_command",
            "arguments": json.dumps({"handle": "shell:0123abcd", "force": False}),
        },
    }
    messages = [
        Message(role="user", content="Stop it."),
        Message(role="assistant", tool_calls=[kill_call]),
        Message(
            role="tool",
            tool_call_id="call_kill",
            tool_name="kill_shell_command",
            content="Process 7 already exited",
        ),
    ]
    kill_wire = next(function for function in CODEX_DIALECT.functions if function.wire_name == "kill_shell_command")
    kill_tool = {"type": "function", "function": {"name": "kill_shell_command", "parameters": kill_wire.parameters}}
    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        monkeypatch.setattr(CodexResponses, "get_async_client", lambda _self: client)
        model = CodexResponses(id="gpt-6.1-sol")
        install_tool_dialect(model, CODEX_DIALECT)
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[kill_tool])

    [call] = [item for item in requests[0]["input"] if item.get("type") == "function_call"]
    assert json.loads(call["arguments"]) == {"session_id": 0x0123ABCD, "force": False}
