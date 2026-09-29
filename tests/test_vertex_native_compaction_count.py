"""Vertex counting accepts a smaller schema than native compaction generation."""
# ruff: noqa: S106

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agno.models.message import Message
from anthropic import AsyncAnthropicVertex
from anthropic.types.beta import BetaMessage
from google.oauth2.credentials import Credentials

from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.test_claude_native_compaction import _CHECKPOINT, _TEXT, _response


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_native_count_uses_vertex_supported_schema_without_changing_generation(*, checkpoint: bool) -> None:
    """The live count endpoint rejects native headers, policy, and checkpoint blocks."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        blocks = [block for message in payload["messages"] for block in message["content"]]
        if (
            "compact-2026-01-12" in request.headers.get("anthropic-beta", "")
            or "compact-2026-01-12" in payload.get("anthropic_beta", [])
            or "context_management" in payload
            or any(block["type"] == "compaction" for block in blocks)
        ):
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "Unsupported count schema"},
                },
            )
        return httpx.Response(200, json={"input_tokens": 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncAnthropicVertex(
            project_id="test-project",
            region="global",
            credentials=Credentials(token="test-token"),
            http_client=http,
            max_retries=0,
        )
        model = MindroomVertexAIClaude(id="claude-sonnet-5", async_client=client)
        model.configure_native_compaction(threshold=50000)
        messages = [Message(role="user", content="Original launch facts.")]
        if checkpoint:
            parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
            messages.extend(
                [
                    Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
                    Message(role="user", content="Continue."),
                ],
            )
        original = [message.model_dump() for message in messages]
        count = await model._count_request_input_tokens(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
        assert count == 100
        assert model.get_request_params()["context_management"]["edits"][0]["type"] == "compact_20260112"
        assert "compact-2026-01-12" in model.get_request_params()["betas"]
        assert [message.model_dump() for message in messages] == original
        if checkpoint:
            assert "Launch port 4321." in json.dumps(requests[-1]["messages"])
            assert "Original launch facts." not in json.dumps(requests[-1]["messages"])
            assert model.native_replay_messages(messages)[0].provider_data["content_blocks"][0] == _CHECKPOINT


_REWRITTEN_BLOCK_TYPES = {"server_tool_use", "tool_search_tool_result", "compaction"}


def _is_rewritten_block(block: dict[str, Any]) -> bool:
    if block.get("type") != "text":
        return False
    try:
        original = json.loads(block["text"])
    except (TypeError, ValueError):
        return False
    return isinstance(original, dict) and original.get("type") in _REWRITTEN_BLOCK_TYPES


@pytest.mark.asyncio
async def test_count_accepts_a_tool_loop_turn_with_thinking_around_tool_search() -> None:
    """Counting must not send a thinking turn whose server-tool blocks were rewritten."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        for message in payload["messages"]:
            content = message["content"] if isinstance(message["content"], list) else []
            if any(block["type"] in _REWRITTEN_BLOCK_TYPES for block in content):
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "Unsupported count schema"},
                    },
                )
            has_thinking = any(block["type"] in {"thinking", "redacted_thinking"} for block in content)
            if "thinking" in payload and has_thinking and any(_is_rewritten_block(block) for block in content):
                message_text = (
                    "`thinking` or `redacted_thinking` blocks in the latest assistant message cannot be modified."
                )
                return httpx.Response(
                    400,
                    json={"type": "error", "error": {"type": "invalid_request_error", "message": message_text}},
                )
        return httpx.Response(200, json={"input_tokens": 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncAnthropicVertex(
            project_id="test-project",
            region="global",
            credentials=Credentials(token="test-token"),
            http_client=http,
            max_retries=0,
        )
        model = MindroomVertexAIClaude(id="claude-opus-5-5", async_client=client, thinking={"type": "adaptive"})
        turn_blocks = [
            {"type": "thinking", "thinking": "Find the tool first.", "signature": "sig-1"},
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_regex",
                "input": {"pattern": "weather"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": "get_weather"}],
                },
            },
            {"type": "thinking", "thinking": "", "signature": "sig-2"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ]
        messages = [
            Message(role="user", content="What is the weather in Paris?"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    },
                ],
                provider_data={"content_blocks": turn_blocks, "signature": "sig-2"},
            ),
            Message(role="tool", content="18C and sunny", tool_call_id="toolu_1", tool_name="get_weather"),
        ]
        original = [message.model_dump() for message in messages]

        weather_tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            },
        }
        count = await model._count_request_input_tokens(
            messages,
            tools=[weather_tool],
            response_format=None,
            compress_tool_results=False,
        )

    assert count == 100
    counted = requests[-1]
    assert "thinking" not in counted
    counted_blocks = [
        block for message in counted["messages"] if isinstance(message["content"], list) for block in message["content"]
    ]
    assert {"type": "text", "text": "Find the tool first."} in counted_blocks
    assert not any(block["type"] in {"thinking", "redacted_thinking"} for block in counted_blocks)
    assert [message.model_dump() for message in messages] == original
