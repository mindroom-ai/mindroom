"""OpenRouter Chat Completions requests carry the Claude cache ladder only for Anthropic-routed models."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.hooks.enrichment import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, ToolSpec, TurnCompleted
from mindroom.system_prompt import render_session_context
from tests.providers.openai_family_support import golden_provider

if TYPE_CHECKING:
    from pathlib import Path

_ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
_FIVE_MINUTES = {"type": "ephemeral"}
_SHARED = "Shared agent instructions.\n"
_TOOLS = [
    ToolSpec(name="first", description="First.", parameters={"type": "object"}),
    ToolSpec(name="second", description="Second.", parameters={"type": "object"}),
]
_USAGE = {
    "prompt_tokens": 5000,
    "completion_tokens": 1,
    "total_tokens": 5001,
    "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 900},
}
_COMPLETION = {
    "id": "gen-test",
    "object": "chat.completion",
    "created": 1,
    "model": "anthropic/claude-haiku-4.5",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": _USAGE,
}
_CHUNK = {"id": "gen-test", "object": "chat.completion.chunk", "created": 1, "model": "anthropic/claude-haiku-4.5"}
_STREAM = [
    {"data": {**_CHUNK, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]}},
    {"data": {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": _USAGE}},
    {"data": "[DONE]"},
]


async def _send(
    tmp_path: Path,
    messages: list[Message],
    *,
    tools: list[ToolSpec] | None = None,
    model_id: str = "anthropic/claude-haiku-4.5",
    stream: bool = False,
    overrides: RequestOverrides | None = None,
    **extra_kwargs: Any,  # noqa: ANN401
) -> tuple[dict[str, Any], TurnCompleted]:
    """Send one request through the OpenRouter route and return its wire body and finished turn."""
    reply: dict[str, Any] = (
        {"status": 200, "headers": {"content-type": "text/event-stream"}, "sse": _STREAM}
        if stream
        else {"status": 200, "headers": {"content-type": "application/json"}, "json": _COMPLETION}
    )
    model_config = {"provider": "openrouter", "id": model_id, "extra_kwargs": {"api_key": "test-key", **extra_kwargs}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    built = golden_provider({"input": {"model_config": model_config}, "response_raw": reply}, tmp_path)
    request = ModelRequest(
        messages=messages,
        tools=tools or (),
        stream=stream,
        overrides=overrides or RequestOverrides(),
    )
    events = [event async for event in built.provider.stream(request)]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return json.loads(built.requests[-1].content), completed


def _conversation(day: str = "Monday") -> list[Message]:
    return [
        Message(role="system", content=_SHARED + render_session_context(f"Current day: {day}.")),
        Message(role="user", content="Look something up."),
        Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "first", "arguments": "{}"}}],
        ),
        Message(role="tool", content="Lookup result.", tool_call_id="call_1"),
        Message(role="assistant", content="Found it."),
        Message(role="user", content="Summarize it."),
    ]


def _markers(payload: object) -> list[dict[str, Any]]:
    """Collect every cache_control marker anywhere in one request body."""
    if isinstance(payload, dict):
        found = [payload["cache_control"]] if "cache_control" in payload else []
        return found + [marker for value in payload.values() for marker in _markers(value)]
    if isinstance(payload, list):
        return [marker for item in payload for marker in _markers(item)]
    return []


@pytest.mark.asyncio
async def test_an_anthropic_route_gets_the_claude_ladder(tmp_path: Path) -> None:
    """The shared system prefix, the two newest message parts, and the last tool carry one TTL within the limit."""
    payload, _ = await _send(tmp_path, _conversation(), tools=_TOOLS)

    messages = payload["messages"]
    assert messages[0]["content"] == [
        {"type": "text", "text": _SHARED, "cache_control": _ONE_HOUR},
        {"type": "text", "text": render_session_context("Current day: Monday.")},
    ]
    assert messages[-1]["content"] == [{"type": "text", "text": "Summarize it.", "cache_control": _ONE_HOUR}]
    assert messages[-2]["content"] == [{"type": "text", "text": "Found it.", "cache_control": _ONE_HOUR}]
    # Unmarked messages keep their original shape.
    assert messages[1]["content"] == "Look something up."
    assert messages[3]["content"] == "Lookup result."
    assert "cache_control" not in payload["tools"][0]
    assert payload["tools"][1]["cache_control"] == _ONE_HOUR
    assert _markers(payload) == [_ONE_HOUR] * 4


@pytest.mark.asyncio
async def test_an_agent_without_instructions_still_caches_its_conversation_and_tools(tmp_path: Path) -> None:
    """With no system prompt, the two newest message parts and the last tool still take the markers."""
    conversation = [message for message in _conversation() if message.role != "system"]

    payload, _ = await _send(tmp_path, conversation, tools=_TOOLS)

    messages = payload["messages"]
    assert messages[0] == {"role": "user", "content": "Look something up."}
    assert messages[-2]["content"] == [{"type": "text", "text": "Found it.", "cache_control": _ONE_HOUR}]
    assert messages[-1]["content"] == [{"type": "text", "text": "Summarize it.", "cache_control": _ONE_HOUR}]
    assert payload["tools"][1]["cache_control"] == _ONE_HOUR
    assert _markers(payload) == [_ONE_HOUR] * 3


@pytest.mark.asyncio
async def test_a_tool_loop_request_extends_the_previous_boundary(tmp_path: Path) -> None:
    """A tool result takes the newest rung so the next loop iteration reads the previous prefix."""
    payload, _ = await _send(tmp_path, _conversation()[:4])

    assert payload["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [{"type": "text", "text": "Lookup result.", "cache_control": _ONE_HOUR}],
    }
    assert payload["messages"][1]["content"] == [
        {"type": "text", "text": "Look something up.", "cache_control": _ONE_HOUR},
    ]
    assert "cache_control" not in json.dumps(payload["messages"][2])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authored", "marker"),
    [("request_params", _ONE_HOUR), ("extra_body", _ONE_HOUR), ("request_params", _FIVE_MINUTES)],
    ids=["request-params", "extra-body", "five-minute"],
)
async def test_an_authored_tool_marker_turns_the_automatic_markers_off(
    tmp_path: Path,
    authored: str,
    marker: dict[str, str],
) -> None:
    """Authored markers stay alone, so automatic 1h markers never exceed the limit or follow a 5m marker."""
    tools = [{"type": "function", "function": {"name": "first", "parameters": {"type": "object"}}}]
    tools[0]["cache_control"] = marker
    messages = _conversation()

    payload, _ = await _send(tmp_path, messages, tools=_TOOLS, **{authored: {"tools": deepcopy(tools)}})

    assert _markers(payload) == [marker]
    assert payload["messages"][0]["content"] == messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_every_request_mode_sends_markers_and_a_request_can_opt_out(tmp_path: Path, *, stream: bool) -> None:
    """Streamed and non-streamed requests get the ladder, and a request without prompt caching sends none."""
    marked, _ = await _send(tmp_path, _conversation(), tools=_TOOLS, stream=stream)
    unmarked, _ = await _send(
        tmp_path / "opted-out",
        _conversation(),
        tools=_TOOLS,
        stream=stream,
        overrides=RequestOverrides(prompt_cache=False),
    )

    assert marked.get("stream", False) is stream
    assert _markers(marked) == [_ONE_HOUR] * 4
    assert _markers(unmarked) == []


@pytest.mark.asyncio
async def test_the_shared_prefix_is_independent_of_the_session_context(tmp_path: Path) -> None:
    """Changing the date leaves the marked shared system part byte-identical."""
    first, _ = await _send(tmp_path / "monday", _conversation("Monday"))
    second, _ = await _send(tmp_path / "tuesday", _conversation("Tuesday"))

    assert first["messages"][0]["content"][0] == second["messages"][0]["content"][0]
    assert first["messages"][0]["content"][1] != second["messages"][0]["content"][1]


@pytest.mark.asyncio
async def test_transient_context_after_the_prompt_takes_no_marker(tmp_path: Path) -> None:
    """Per-request context must not take the breakpoint that later turns replay."""
    transient = render_transient_context(["Current memory."])
    messages = [
        Message(role="system", content=_SHARED),
        Message(role="user", content="Durable prompt."),
        Message(role="user", content=transient),
    ]

    payload, _ = await _send(tmp_path, messages)

    assert payload["messages"][1:] == [
        {"role": "user", "content": [{"type": "text", "text": "Durable prompt.", "cache_control": _ONE_HOUR}]},
        {"role": "user", "content": transient},
    ]


@pytest.mark.asyncio
async def test_multimodal_user_content_marks_its_text_part(tmp_path: Path) -> None:
    """Image parts stay unmarked while the newest text part in the message takes the rung."""
    content = [
        {"type": "text", "text": "Describe this."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    messages = [Message(role="system", content=_SHARED), Message(role="user", content=deepcopy(content))]

    payload, _ = await _send(tmp_path, messages)

    assert payload["messages"][1]["content"] == [
        {"type": "text", "text": "Describe this.", "cache_control": _ONE_HOUR},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert messages[1].content == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id",
    ["deepseek/deepseek-v4.1-flash", "openai/gpt-6-astra", "z-ai/glm-5.3", "google/gemini-3.8-flash"],
)
async def test_other_model_families_keep_the_plain_request(tmp_path: Path, model_id: str) -> None:
    """Implicitly cached upstreams keep the plain request format."""
    messages = _conversation()

    payload, _ = await _send(tmp_path, messages, tools=_TOOLS, model_id=model_id)

    assert _markers(payload) == []
    assert payload["messages"][0]["content"] == messages[0].content


@pytest.mark.asyncio
async def test_the_ladder_follows_the_cache_settings(tmp_path: Path) -> None:
    """``cache_system_prompt: false`` sends no markers and ``extended_cache_time: false`` the 5-minute lifetime."""
    disabled, _ = await _send(tmp_path / "disabled", _conversation(), tools=_TOOLS, cache_system_prompt=False)
    short, _ = await _send(
        tmp_path / "short",
        _conversation(),
        tools=_TOOLS,
        model_id="~anthropic/claude-sonnet-latest",
        extended_cache_time=False,
    )

    assert _markers(disabled) == []
    assert _markers(short) == [_FIVE_MINUTES] * 4


@pytest.mark.asyncio
async def test_history_and_tool_definitions_are_not_mutated(tmp_path: Path) -> None:
    """Markers are added to request copies, never to stored messages or reusable tool definitions."""
    messages = _conversation()
    snapshot = [message.to_dict() for message in messages]
    tools = list(_TOOLS)

    await _send(tmp_path, messages, tools=tools)

    assert [message.to_dict() for message in messages] == snapshot
    assert tools == _TOOLS


@pytest.mark.asyncio
async def test_cache_reads_and_writes_reach_the_usage(tmp_path: Path) -> None:
    """OpenRouter's cached and cache-write prompt tokens become MindRoom's cache usage."""
    _, completed = await _send(tmp_path, _conversation())

    assert (
        completed.message.metrics.tokens.cache_read_tokens,
        completed.message.metrics.tokens.cache_write_tokens,
    ) == (4000, 900)


@pytest.mark.asyncio
async def test_caching_needs_no_configuration(tmp_path: Path) -> None:
    """An OpenRouter Claude model caches with the one-hour lifetime unless its config opts out."""
    payload, _ = await _send(tmp_path, [Message(role="system", content=_SHARED), Message(role="user", content="Hi")])

    assert _markers(payload) == [_ONE_HOUR, _ONE_HOUR]
