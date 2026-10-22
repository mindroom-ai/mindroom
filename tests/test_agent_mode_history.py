"""Real provider request construction preserves paired history across mode changes."""

from __future__ import annotations

import base64
import copy
import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, complete
from tests.providers.golden_support import Wire
from tests.providers.test_provider_families import _PROVIDER_ENV_PREFIXES, FAMILIES

if TYPE_CHECKING:
    from pathlib import Path

_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
)


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _history(family: str, image_path: Path) -> list[Message]:
    """A standard-mode tool pair, a minimal-mode Bash pair, and a new prompt with an image."""
    standard_call = _call("old", "read_file", '{"path":"notes.txt"}')
    standard_turn = Message(role="assistant", tool_calls=[standard_call], from_history=True)
    if family == "gemini":
        standard_call["thought_signature"] = base64.b64encode(b"retained signature").decode()
    if family == "anthropic":
        standard_turn.provider_data = {
            "content_blocks": [{"type": "tool_use", "id": "old", "name": "read_file", "input": {"path": "notes.txt"}}],
        }
    return [
        Message(role="user", content="Earlier task", from_history=True),
        standard_turn,
        Message(role="tool", tool_call_id="old", tool_name="read_file", content="old contents", from_history=True),
        Message(
            role="assistant",
            tool_calls=[_call("minimal", "bash", '{"command":"agent tools call"}')],
            from_history=True,
        ),
        Message(role="tool", tool_call_id="minimal", tool_name="bash", content="current result", from_history=True),
        Message(role="user", content="Continue", images=[Image(filepath=str(image_path), mime_type="image/png")]),
    ]


# Each reader returns the tool-call ids, the tool-result ids, and whether an image went out in one request body.
type _Sent = tuple[set[str], set[str], bool]


def _chat_sent(body: dict[str, Any]) -> _Sent:
    messages = body["messages"]
    calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
    results = {message["tool_call_id"] for message in messages if "tool_call_id" in message}
    blocks = [block for message in messages if isinstance(message["content"], list) for block in message["content"]]
    return calls, results, any(block["type"] == "image_url" for block in blocks)


def _claude_sent(body: dict[str, Any]) -> _Sent:
    blocks = [
        block for message in body["messages"] if isinstance(message["content"], list) for block in message["content"]
    ]
    calls = {block["id"] for block in blocks if block["type"] == "tool_use"}
    results = {block["tool_use_id"] for block in blocks if block["type"] == "tool_result"}
    return calls, results, any(block["type"] == "image" for block in blocks)


def _gemini_sent(body: dict[str, Any]) -> _Sent:
    parts = [part for content in body["contents"] for part in content["parts"]]
    calls = {part["functionCall"].get("id") or part["functionCall"]["name"] for part in parts if "functionCall" in part}
    results = {
        part["functionResponse"].get("id") or part["functionResponse"]["name"]
        for part in parts
        if "functionResponse" in part
    }
    return calls, results, any("inlineData" in part for part in parts)


_SENT = {"openai_chat": _chat_sent, "anthropic": _claude_sent, "gemini": _gemini_sent}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(_SENT))
async def test_standard_minimal_standard_requests_keep_paired_history(
    family: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each mode offers one tool, yet every request carries both stored pairs and the image, and history is untouched."""
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)
    spec = FAMILIES[family]
    if family == "anthropic":
        # A current model id, so the SDK raises no deprecation warning.
        spec = replace(spec, config={**spec.config, "id": "claude-sonnet-5-5"})
    wire = Wire(lambda _request: spec.reply())
    provider = spec.adapter(tmp_path, wire)
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(_PIXEL_PNG)
    history = _history(family, image_path)
    before = copy.deepcopy(history)
    tool_names = ("read_file", "bash", "read_file")

    for name in tool_names:
        tool = ToolSpec(name=name, description="available", parameters={"type": "object", "properties": {}})
        await complete(provider, ModelRequest(messages=history, tools=[tool]))
        assert history == before

    for index, name in enumerate(tool_names):
        body = wire.body(index)
        assert list(spec.wire_tools(body)) == [name]
        calls, results, image = _SENT[family](body)
        assert len(calls) == 2, "both stored tool calls are sent"
        assert results == calls, "every stored tool result keeps its call"
        assert image
