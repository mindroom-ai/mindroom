"""Legacy manual thinking remains valid when native replay falls back."""
# ruff: noqa: S106

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agno.models.message import Message
from anthropic import AsyncAnthropic, AsyncAnthropicVertex
from anthropic.types.beta import BetaMessage
from google.oauth2.credentials import Credentials

from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.test_claude_native_compaction import _CHECKPOINT, _TEXT, _response, _stream_response


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Using Claude with claude-opus-4-6.*:UserWarning")
@pytest.mark.parametrize("model_id", ["claude-sonnet-4-6", "claude-opus-4-6"])
@pytest.mark.parametrize("vertex", [False, True])
@pytest.mark.parametrize("source", ["typed", "top", "raw"])
@pytest.mark.parametrize("stream", [False, True])
async def test_manual_thinking_survives_checkpoint_fallback(
    model_id: str,
    *,
    vertex: bool,
    source: str,
    stream: bool,
) -> None:
    """Removing required thinking makes the outgoing legacy tool continuation invalid."""
    requests: list[dict[str, Any]] = []
    counts: list[int] = []
    thinking = {"type": "thinking", "thinking": "Need a lookup.", "signature": "original-signature"}
    tool_use = {"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}
    manual = {"type": "enabled", "budget_tokens": 1024}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "count-tokens" in request.url.path:
            assert payload["thinking"] == manual
            blocks = [block for message in payload["messages"] for block in message["content"]]
            tokens = 1000 if "Original launch facts." in json.dumps(blocks) else 20000
            counts.append(tokens)
            return httpx.Response(200, json={"input_tokens": tokens})
        requests.append(payload)
        if payload.get("stream"):
            return _stream_response([_TEXT])
        blocks = [thinking, tool_use] if len(requests) == 1 else [_TEXT]
        stop_reason = "tool_use" if len(requests) == 1 else "end_turn"
        return httpx.Response(200, json={**_response(blocks, stop_reason=stop_reason), "model": model_id})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = (
            AsyncAnthropicVertex(
                project_id="test-project",
                region="global",
                credentials=Credentials(token="test-token"),
                http_client=http_client,
            )
            if vertex
            else AsyncAnthropic(api_key="test-key", http_client=http_client)
        )
        model_type = MindroomVertexAIClaude if vertex else MindRoomAnthropicClaude
        model = model_type(
            id=model_id,
            async_client=client,
            max_tokens=4096,
            thinking=manual if source == "typed" else {"type": "adaptive"},
            request_params=(
                {"extra_body": {"thinking": manual}}
                if source == "raw"
                else {"thinking": manual}
                if source == "top"
                else None
            ),
        )
        model.configure_native_compaction(threshold=60000)
        anchor = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
        messages = [
            Message(role="user", content="Original launch facts.", from_history=True),
            Message(role="assistant", content=anchor.content, provider_data=anchor.provider_data, from_history=True),
            Message(role="user", content="Look up the status."),
        ]
        parsed = await model.ainvoke(messages, Message(role="assistant"))
        assistant = Message(
            role="assistant",
            content=parsed.content,
            provider_data=parsed.provider_data,
            tool_calls=parsed.tool_calls,
            reasoning_content=parsed.reasoning_content,
        )
        messages.extend(
            [
                Message.model_validate_json(assistant.model_dump_json()),
                Message(role="tool", tool_call_id="toolu_lookup", content="Found it. " * 5000),
            ],
        )
        original = [message.model_dump() for message in messages]
        if vertex:
            model.context_window = 10000
        else:
            model.configure_native_compaction(threshold=None)
        if stream:
            async for _ in model.ainvoke_stream(messages, Message(role="assistant")):
                pass
        else:
            await model.ainvoke(messages, Message(role="assistant"))

    assert model.native_compaction is None
    assert requests[-1]["thinking"] == manual
    latest_assistant = next(message for message in reversed(requests[-1]["messages"]) if message["role"] == "assistant")
    assert latest_assistant["content"] == [thinking, tool_use]
    assert "compaction" not in {block["type"] for message in requests[-1]["messages"] for block in message["content"]}
    assert [message.model_dump() for message in messages] == original
    if vertex:
        assert counts == [20000, 1000]


@pytest.mark.parametrize(
    ("request_params", "expected_thinking", "keep_thinking"),
    [
        ({"thinking": {"type": "adaptive"}}, {"type": "adaptive"}, False),
        ({"extra_body": {"thinking": None}}, None, False),
        ({"extra_body": {"thinking": {"type": "adaptive"}}}, {"type": "adaptive"}, False),
        (
            {"thinking": {"type": "adaptive"}, "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 1024}}},
            {"type": "enabled", "budget_tokens": 1024},
            True,
        ),
    ],
)
def test_thinking_overrides_align_replay_and_vertex_counting(
    request_params: dict[str, Any],
    expected_thinking: dict[str, Any] | None,
    *,
    keep_thinking: bool,
) -> None:
    """Wrong override precedence either drops required reasoning or counts a different mode."""
    model = MindroomVertexAIClaude(
        id="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 1024},
        request_params=request_params,
    )
    redacted = {"type": "redacted_thinking", "data": "unchanged-redacted-thinking"}
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, redacted, _TEXT])))
    messages = [
        Message(role="user", content="Original facts."),
        Message(
            role="assistant",
            content=parsed.content,
            provider_data=parsed.provider_data,
            redacted_reasoning_content=parsed.redacted_reasoning_content,
        ),
        Message(role="user", content="Continue."),
    ]
    payload = model._request_input_kwargs(messages, tools=None, response_format=None, compress_tool_results=False)
    assert payload.get("thinking") == expected_thinking
    assert (redacted in payload["messages"][1]["content"]) is keep_thinking
    assert messages[1].provider_data["content_blocks"][1] == redacted
