"""Real provider request construction preserves paired history across mode changes."""

# ruff: noqa: ANN001, ANN202, D103, PLC0415
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from agno.media import Image
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.tools.function import Function
from pydantic import BaseModel


def _assert_pairs(request, provider):  # noqa: C901, PLR0912
    calls = set()
    results = set()
    if provider == "openai":
        for message in request["messages"]:
            calls.update(tool["id"] for tool in message.get("tool_calls", []))
            if message.get("tool_call_id"):
                results.add(message["tool_call_id"])
    elif provider == "anthropic":
        for message in request["messages"]:
            for item in message["content"] if isinstance(message["content"], list) else []:
                block = item.model_dump() if isinstance(item, BaseModel) else item
                if block["type"] == "tool_use":
                    calls.add(block["id"])
                elif block["type"] == "tool_result":
                    results.add(block["tool_use_id"])
    else:
        for message in request["contents"]:
            for part in message.parts:
                if part.function_call:
                    calls.add(part.function_call.id or part.function_call.name)
                elif part.function_response:
                    results.add(part.function_response.id or part.function_response.name)
    assert results == calls, "Fake provider rejects dangling tool results"
    assert len(results) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
async def test_standard_minimal_standard_actual_provider_requests_preserve_history(
    provider,
    monkeypatch,
    tmp_path,
) -> None:
    from agno.models.anthropic import Claude
    from agno.models.google import Gemini
    from agno.models.openai import OpenAIChat

    model = {"openai": OpenAIChat, "anthropic": Claude, "gemini": Gemini}[provider]()
    requests = []

    async def send(**request):  # noqa: ANN003
        _assert_pairs(request, provider)
        requests.append(request)
        return object()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=send)),
        messages=SimpleNamespace(create=send),
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=send)),
    )
    monkeypatch.setattr(type(model), "get_client" if provider == "gemini" else "get_async_client", lambda _self: client)
    monkeypatch.setattr(type(model), "_parse_provider_response", lambda *_args, **_kwargs: ModelResponse(content="ok"))
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
        ),
    )
    history = [
        Message(role="user", content="Earlier task"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "old",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"notes.txt"}',
                    },
                },
            ],
        ),
        Message(role="tool", tool_call_id="old", tool_name="read_file", content="old contents"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "minimal",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"agent tools call"}',
                    },
                },
            ],
        ),
        Message(role="tool", tool_call_id="minimal", tool_name="bash", content="current result"),
        Message(role="user", content="Continue", images=[Image(filepath=image_path, mime_type="image/png")]),
    ]
    if provider == "gemini":
        history[1].tool_calls[0]["thought_signature"] = base64.b64encode(b"retained signature").decode()
    if provider == "anthropic":
        history[1].provider_data = {
            "content_blocks": [{"type": "tool_use", "id": "old", "name": "read_file", "input": {"path": "notes.txt"}}],
        }
    for message in history[:-1]:
        message.from_history = True
    before = json.dumps([message.model_dump(mode="json") for message in history], sort_keys=True)
    for name in ("read_file", "bash", "read_file"):
        await model.ainvoke(
            history,
            Message(role="assistant"),
            tools=[
                {
                    "type": "function",
                    "function": Function(name=name, description="available").to_dict(),
                },
            ],
        )
        after = json.dumps([message.model_dump(mode="json") for message in history], sort_keys=True)
        assert after == before
    for request, name in zip(requests, ("read_file", "bash", "read_file"), strict=True):
        if provider == "gemini":
            declarations = request["config"].tools[0].function_declarations
            assert [item.name for item in declarations] == [name]
            assert any(part.inline_data for message in request["contents"] for part in message.parts)
        elif provider == "openai":
            assert [item["function"]["name"] for item in request["tools"]] == [name]
            assert any(
                block.get("type") == "image_url"
                for message in request["messages"]
                for block in (message["content"] if isinstance(message["content"], list) else [])
            )
        else:
            assert [item["name"] for item in request["tools"]] == [name]
            assert any(
                (block.type if hasattr(block, "type") else block.get("type")) == "image"
                for message in request["messages"]
                for block in (message["content"] if isinstance(message["content"], list) else [])
            )
