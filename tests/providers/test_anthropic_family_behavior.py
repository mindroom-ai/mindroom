"""Behavior of the owned Claude adapter beyond the goldens: settings, events, stop reasons, errors, overrides, replay."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import anthropic
import boto3.session
import google.auth
import google.auth.exceptions
import httpx
import pytest
from google.oauth2.credentials import Credentials
from pydantic import BaseModel

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.llm.errors import (
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.media import Audio, File, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    ModelRequest,
    ReasoningDelta,
    RequestOverrides,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.providers.anthropic.messages import build_provider
from mindroom.providers.anthropic.settings import parse_claude_settings
from mindroom.providers.anthropic.wire import supports_prefill
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.system_prompt import render_session_context
from tests.providers.anthropic_family_support import (
    CLAUDE_USAGE,
    ROUTE_CONFIGS,
    claude_events,
    claude_message,
    claude_turn,
    isolated_provider_environment,
    json_reply,
    provider_for,
    route_provider,
    sse_reply,
)
from tests.providers.golden_support import Wire, pipeline_provider, sse_response

if TYPE_CHECKING:
    from pathlib import Path

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
)
_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


def _user(text: str = "Hello") -> list[Message]:
    return [Message(role="system", content="Be brief."), Message(role="user", content=text)]


def test_unknown_extra_kwargs_are_rejected_with_the_accepted_keys() -> None:
    """Dropped Agno features and another route's keys fail at construction and name what is accepted."""
    with pytest.raises(ValueError, match=r"skills.*Accepted keys: .*cache_system_prompt") as skills:
        parse_claude_settings("anthropic", {"skills": [{"type": "anthropic", "skill_id": "pptx"}]})
    assert "mcp_servers" not in str(skills.value)
    with pytest.raises(ValueError, match="project_id"):
        parse_claude_settings("anthropic", {"project_id": "p"})
    assert parse_claude_settings("vertexai_claude", {"project_id": "p", "region": "global"}).region == "global"
    settings = parse_claude_settings("bedrock_claude", {"aws_profile": "dev", "cache_system_prompt": False})
    assert (settings.aws_profile, settings.cache_system_prompt, settings.extended_cache_time) == ("dev", False, True)
    assert settings.max_tokens == 8192
    assert settings.timeout == 3600.0
    assert parse_claude_settings("anthropic", {"timeout": 120.0}).timeout == 120.0


@pytest.mark.parametrize(
    ("route", "provider_name", "structured_output", "native_tool_search"),
    [
        ("anthropic", "Anthropic", "native", True),
        ("vertexai_claude", "VertexAI", "json_mode", True),
        ("bedrock_claude", "AwsBedrock", "json_mode", False),
    ],
)
def test_identity_and_capabilities_per_route(
    tmp_path: Path,
    route: str,
    provider_name: str,
    structured_output: str,
    native_tool_search: bool,
) -> None:
    """Identity strings stay what stored runs and usage rows carry, and capabilities replace class probes."""
    provider = route_provider(route, tmp_path, Wire(json_reply([])))
    assert (provider.identity.provider, provider.identity.provider_name) == (provider_name, provider_name)
    capabilities = provider.capabilities
    assert capabilities.structured_output == structured_output
    assert capabilities.native_tool_search is native_tool_search
    assert capabilities.omitted_media == frozenset({"audio", "video"})
    assert capabilities.claude_family is True
    assert capabilities.provider_default_sampling is True
    assert capabilities.native_compaction is (route != "bedrock_claude")


@pytest.mark.asyncio
async def test_stream_reports_start_usage_deltas_tool_calls_and_the_finished_message(tmp_path: Path) -> None:
    """Partial usage comes first and again with the message delta, and the turn ends with the parsed message."""
    wire = Wire(
        sse_reply(
            [
                {"type": "thinking", "thinking": "Check first.", "signature": "sig"},
                {"type": "text", "text": "Looking it up."},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
            ],
            stop_reason="tool_use",
        ),
    )
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    tools = [ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object", "properties": {}})]
    streamed = await claude_events(provider, ModelRequest(messages=_user(), tools=tools))

    # Block starts, the signature delta, and the tool-call argument piece show up as provider activity.
    events = [event for event in streamed if not isinstance(event, StreamProgress)]
    assert len(streamed) - len(events) >= 3
    assert isinstance(events[0], UsageReported)
    assert events[0].final is False
    assert (
        events[0].usage.tokens.input_tokens,
        events[0].usage.tokens.output_tokens,
        events[0].usage.tokens.cache_read_tokens,
    ) == (
        40,
        1,
        5,
    )
    assert events[1] == ReasoningDelta("Check first.")
    assert events[2] == TextDelta("Looking it up.")
    assert events[3] == ToolCallReady(
        {"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
    )
    delta_usage, final_usage, completed = events[4], events[5], events[6]
    assert isinstance(delta_usage, UsageReported)
    assert (delta_usage.final, delta_usage.usage.tokens.input_tokens, delta_usage.usage.tokens.output_tokens) == (
        False,
        40,
        9,
    )
    assert isinstance(final_usage, UsageReported)
    assert final_usage.final is True
    assert isinstance(completed, TurnCompleted)
    assert completed.stop_reason == "tool_use"
    message = completed.message
    assert final_usage.usage is message.metrics
    assert (
        message.metrics.tokens.input_tokens,
        message.metrics.tokens.output_tokens,
        message.metrics.tokens.total_tokens,
    ) == (40, 9, 49)
    assert (message.metrics.tokens.cache_read_tokens, message.metrics.tokens.cache_write_tokens) == (5, 3)
    assert message.metrics.provider_metrics == {
        "context_usage": {"input_tokens": 40, "cache_read_tokens": 5, "cache_write_tokens": 3},
    }
    assert message.metrics.duration is not None
    assert message.metrics.time_to_first_token is not None
    assert message.content == "Looking it up."
    assert message.reasoning_content == "Check first."
    assert message.tool_calls == [events[3].call]
    provider_data = message.provider_data or {}
    assert provider_data["signature"] == "sig"
    assert provider_data["stop_reason"] == "tool_use"
    assert provider_data["mindroom_model"] == {"id": "claude-sonnet-5-5", "provider": "Anthropic"}
    assert provider_data["mindroom_native_compaction"] is None
    assert [block["type"] for block in provider_data["content_blocks"]] == ["thinking", "text", "tool_use"]
    assert wire.body()["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude", "bedrock_claude"])
async def test_a_whole_reply_finishes_with_the_message_its_stream_would(tmp_path: Path, route: str) -> None:
    """A non-streaming reply yields only its usage and finished turn, whose message matches the streamed one."""
    blocks = [
        {"type": "thinking", "thinking": "Check first.", "signature": "sig"},
        {"type": "text", "text": "Looking it up."},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
    ]
    tools = [ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object", "properties": {}})]
    streamed = await claude_events(
        route_provider(route, tmp_path, Wire(sse_reply(blocks, stop_reason="tool_use"))),
        ModelRequest(messages=_user(), tools=tools),
    )
    whole = await claude_events(
        route_provider(route, tmp_path, Wire(json_reply(blocks, stop_reason="tool_use"))),
        ModelRequest(messages=_user(), tools=tools, stream=False),
    )

    assert [type(event) for event in whole] == [UsageReported, TurnCompleted]
    streamed_turn, whole_turn = streamed[-1], whole[-1]
    assert isinstance(streamed_turn, TurnCompleted)
    assert isinstance(whole_turn, TurnCompleted)
    assert [
        (turn.message.content, turn.message.reasoning_content, turn.message.tool_calls)
        for turn in (streamed_turn, whole_turn)
    ] == [("Looking it up.", "Check first.", streamed_turn.message.tool_calls)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_stored_reasoning_is_what_agno_stored_for_the_stream_mode(tmp_path: Path, *, stream: bool) -> None:
    """A streamed reply stores its thinking blocks joined, a blocking reply only the last, as Agno did.

    Agno summed a stream's thinking and redacted thinking deltas, and its response parser kept the last block of each.
    """
    blocks = [
        {"type": "thinking", "thinking": "First. ", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "redacted-1"},
        {"type": "thinking", "thinking": "Second.", "signature": "sig-2"},
        {"type": "redacted_thinking", "data": "redacted-2"},
        {"type": "text", "text": "Done."},
    ]
    reply = sse_reply(blocks) if stream else json_reply(blocks)
    provider = route_provider("anthropic", tmp_path, Wire(reply), extra_kwargs={"thinking": {"type": "adaptive"}})

    message = (await claude_turn(provider, ModelRequest(messages=_user(), stream=stream))).message

    expected = ("First. Second.", "redacted-1redacted-2") if stream else ("Second.", "redacted-2")
    assert (message.reasoning_content, message.redacted_reasoning_content) == expected
    assert (message.provider_data or {})["signature"] == "sig-2"


@pytest.mark.asyncio
async def test_a_whole_reply_stores_its_last_thinking_block_where_a_stream_stores_them_all(tmp_path: Path) -> None:
    """Agno's whole-response parser kept the last thinking block, while its stream summed every block's deltas."""
    blocks = [
        {"type": "thinking", "thinking": "First. ", "signature": "sig-1"},
        {"type": "thinking", "thinking": "Second.", "signature": "sig-2"},
        {"type": "text", "text": "Done."},
    ]
    reasoning = {}
    for stream, reply in ((True, sse_reply(blocks)), (False, json_reply(blocks))):
        provider = route_provider("anthropic", tmp_path, Wire(reply), extra_kwargs={"thinking": {"type": "adaptive"}})
        turn = await claude_turn(provider, ModelRequest(messages=_user(), stream=stream))
        reasoning[stream] = turn.message.reasoning_content

    assert reasoning == {True: "First. Second.", False: "Second."}


@pytest.mark.asyncio
@pytest.mark.parametrize("cumulative", [False, True], ids=["output_only", "cumulative"])
async def test_a_completed_stream_counts_its_usage_once(tmp_path: Path, *, cumulative: bool) -> None:
    """Input usage reported at the start is not counted again when the final delta repeats it."""
    start_usage = {"input_tokens": 1200, "cache_read_input_tokens": 48000, "cache_creation_input_tokens": 800}
    delta_usage = {**start_usage, "output_tokens": 50} if cumulative else {"output_tokens": 50}
    events = [
        {"type": "message_start", "message": claude_message([], usage={**start_usage, "output_tokens": 1})},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Ready"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": delta_usage},
        {"type": "message_stop"},
    ]
    wire = Wire(sse_response(events, named=True))

    completed = await claude_turn(route_provider("anthropic", tmp_path, wire), ModelRequest(messages=_user()))

    metrics = completed.message.metrics
    assert (
        metrics.tokens.input_tokens,
        metrics.tokens.output_tokens,
        metrics.tokens.cache_read_tokens,
        metrics.tokens.cache_write_tokens,
    ) == (
        1200,
        50,
        48000,
        800,
    )


@pytest.mark.asyncio
async def test_service_tier_and_server_tool_requests_are_kept_with_the_usage(tmp_path: Path) -> None:
    """The direct API reports its service tier, and a web search its request count, beside the token counts."""
    usage = {**CLAUDE_USAGE, "service_tier": "standard", "server_tool_use": {"web_search_requests": 2}}
    wire = Wire(json_reply([{"type": "text", "text": "Sunny."}], usage=usage))
    completed = await claude_turn(
        route_provider("anthropic", tmp_path, wire),
        ModelRequest(messages=_user(), stream=False),
    )
    assert completed.message.metrics.provider_metrics == {
        "server_tool_use": {"web_fetch_requests": None, "web_search_requests": 2},
        "service_tier": "standard",
        "context_usage": {"input_tokens": 40, "cache_read_tokens": 5, "cache_write_tokens": 3},
    }


@pytest.mark.asyncio
async def test_cut_off_response_keeps_its_tool_calls_and_stops_at_the_output_limit(tmp_path: Path) -> None:
    """A call the output limit cut off is kept with the arguments that parsed, so the loop can answer it."""
    truncated = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "save_file",
        "input": {},
        "partial_json": '{"name": "notes.md", "content": "# Not',
    }
    wire = Wire(sse_reply([truncated], stop_reason="max_tokens"))
    completed = await claude_turn(route_provider("anthropic", tmp_path, wire), ModelRequest(messages=_user()))
    assert completed.stop_reason == "output_limit"
    assert completed.message.tool_calls == [
        {"id": "toolu_1", "type": "function", "function": {"name": "save_file", "arguments": '{"name": "notes.md"}'}},
    ]
    assert (completed.message.provider_data or {})["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_block_types_the_sdk_does_not_know_are_kept_for_replay(tmp_path: Path, stream: bool) -> None:
    """A newer block type, which the SDK builds as a text block with its own ``type``, is kept, not read as text."""
    unknown = {"type": "future_block", "payload": "p"}
    text = {"type": "text", "text": "Hi."}
    if stream:
        reply = sse_reply([{**unknown, "deltas": [{"type": "future_delta", "payload": "q"}]}, dict(text)])
    else:
        reply = json_reply([unknown, text])
    events = await claude_events(
        route_provider("anthropic", tmp_path, Wire(reply)),
        ModelRequest(messages=_user(), stream=stream),
    )
    outputs = [event for event in events if isinstance(event, (TextDelta, ReasoningDelta, ToolCallReady))]
    assert outputs == ([TextDelta("Hi.")] if stream else [])
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.message.content == "Hi."
    provider_data = completed.message.provider_data or {}
    assert provider_data["content_blocks"] == [unknown, text]
    assert [(block["type"], block["payload"]) for block in provider_data["server_tool_blocks"]] == [
        ("future_block", "p"),
    ]


@pytest.mark.asyncio
async def test_refusal_completes_with_the_refusal_stop_reason(tmp_path: Path) -> None:
    """A safeguard refusal is a completed turn the loop recognizes by its stop reason, not an adapter error."""
    wire = Wire(json_reply([], stop_reason="refusal"))
    provider = route_provider("anthropic", tmp_path, wire)
    completed = await claude_turn(provider, ModelRequest(messages=_user(), stream=False))
    assert completed.stop_reason == "refusal"
    assert completed.message.content is None
    assert completed.message.metrics.tokens.input_tokens == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected", "retryable"),
    [
        (429, {"type": "rate_limit_error", "message": "Rate limited"}, RateLimited, True),
        (529, {"type": "overloaded_error", "message": "Overloaded"}, ProviderUnavailable, True),
        (503, {"type": "api_error", "message": "Unavailable"}, ProviderUnavailable, True),
        (
            400,
            {"type": "invalid_request_error", "message": "prompt is too long: 300000 tokens"},
            ContextWindowExceeded,
            False,
        ),
        (400, {"type": "invalid_request_error", "message": "messages.0: bad"}, InvalidRequest, False),
        (401, {"type": "authentication_error", "message": "invalid x-api-key"}, ProviderError, False),
    ],
)
async def test_sdk_status_errors_map_to_the_shared_provider_errors(
    tmp_path: Path,
    status: int,
    body: dict[str, Any],
    expected: type[Exception],
    retryable: bool,
) -> None:
    """Each SDK failure becomes the shared error class whose retryable flag the loop decides from."""
    wire = Wire(httpx.Response(status, json={"type": "error", "error": body}))
    provider = route_provider("anthropic", tmp_path, wire)
    request = ModelRequest(messages=_user(), overrides=RequestOverrides(caller_retries=True))
    with pytest.raises(expected) as raised:
        await claude_turn(provider, request)
    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert raised.value.provider == "Anthropic"
    assert body["message"] in str(raised.value)
    assert len(wire.requests) == 1, "a caller that retries itself must stop the SDK's own retries"


def _stream_failing_with(error_type: str) -> httpx.Response:
    """A stream that starts the message, then sends an ``error`` event of ``error_type``."""
    start = claude_message([], usage={**CLAUDE_USAGE, "output_tokens": 1}) | {"stop_reason": None}
    error = {"type": "error", "error": {"type": error_type, "message": "Internal server error"}}
    return sse_response([{"type": "message_start", "message": start}, error], named=True)


@pytest.mark.asyncio
async def test_a_mid_stream_api_error_before_output_reissues_the_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``api_error`` event after the 200 status, before any output, is retried like the same HTTP failure."""
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    wire = Wire(_stream_failing_with("api_error"), sse_reply([{"type": "text", "text": "hello world"}]))
    provider = pipeline_provider(route_provider("anthropic", tmp_path, wire))

    with isolated_provider_environment():
        turn = await complete(provider, ModelRequest(messages=_user()))

    assert (turn.content, len(wire.requests)) == ("hello world", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("overloaded_error", ProviderUnavailable),
        ("api_error", ProviderUnavailable),
        ("timeout_error", ProviderUnavailable),
        ("rate_limit_error", RateLimited),
        ("invalid_request_error", ProviderError),
    ],
)
async def test_a_mid_stream_error_event_is_classified_by_its_error_type(
    tmp_path: Path,
    error_type: str,
    expected: type[ProviderError],
) -> None:
    """The SDK reports a stream's error event with the 200 status, so the event's error type decides the error."""
    wire = Wire(_stream_failing_with(error_type))
    provider = route_provider("anthropic", tmp_path, wire)

    with pytest.raises(ProviderError) as raised:
        await claude_turn(provider, ModelRequest(messages=_user()))

    assert type(raised.value) is expected
    assert (raised.value.status_code, raised.value.retryable) == (None, expected is not ProviderError)


@pytest.mark.asyncio
async def test_transport_failures_are_retryable_provider_unavailable(tmp_path: Path) -> None:
    """A connection that never reaches the provider is retryable."""

    def fail(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    provider = route_provider("anthropic", tmp_path, Wire(fail))
    with pytest.raises(ProviderUnavailable) as raised:
        await claude_turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(caller_retries=True)))
    assert raised.value.retryable is True
    assert raised.value.status_code is None


@pytest.mark.asyncio
async def test_summary_overrides_drop_thinking_cache_markers_and_apply_sampling(tmp_path: Path) -> None:
    """One-off summary requests run without thinking or cache writes, with their own limits."""
    wire = Wire(sse_reply([{"type": "text", "text": "Summary."}]))
    provider = route_provider(
        "anthropic",
        tmp_path,
        wire,
        id="claude-sonnet-4-5",
        extra_kwargs={
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "temperature": 0.7,
            "max_tokens": 512,
            "request_params": {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 2048}, "top_k": 4}},
        },
    )
    overrides = RequestOverrides(temperature=0.1, prompt_cache=False, disable_thinking=True)
    messages = [*_user("Summarize."), Message(role="assistant", content="Earlier answer.")]
    await claude_turn(provider, ModelRequest(messages=messages, overrides=overrides))
    body = wire.body()
    assert "thinking" not in body
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.1
    assert body["top_k"] == 4
    assert "cache_control" not in json.dumps(body)
    # Sonnet 4.5 accepts prefill, and thinking is off, so the history may end with the assistant turn.
    assert body["messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_default_sampling_models_lose_sampling_controls_everywhere(tmp_path: Path) -> None:
    """The newest Claude generations reject non-default sampling, so authored and overriding values are dropped."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider(
        "anthropic",
        tmp_path,
        wire,
        extra_kwargs={"temperature": 0.7, "top_p": 0.9, "request_params": {"extra_body": {"top_k": 3}}},
    )
    await claude_turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(temperature=0.2)))
    body = wire.body()
    assert not {"temperature", "top_p", "top_k"} & body.keys()
    # The conversation ends with a user turn already, and thinking is off, yet Sonnet 5.5 rejects prefill.
    assert body["messages"][-1]["content"][0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_history_ending_with_an_assistant_turn_gets_a_trailing_user_turn(tmp_path: Path) -> None:
    """Models without prefill, and any model with thinking on, need the request to end with a user turn."""
    wire = Wire(sse_reply([{"type": "text", "text": "More."}]))
    provider = route_provider("anthropic", tmp_path, wire)
    stored = {
        "content_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "A"}],
    }
    messages = [
        *_user(),
        Message(role="assistant", content="A", provider_data=stored),
        Message(role="assistant", content="B"),
    ]
    await claude_turn(provider, ModelRequest(messages=messages))
    turns = wire.body()["messages"]
    assert turns[-1]["role"] == "user"
    assert [(block["type"], block["text"]) for block in turns[-1]["content"]] == [("text", "continue")]
    # Two assistant turns share one message only without the earlier turn's thinking.
    assert [(block["type"], block["text"]) for block in turns[-2]["content"]] == [("text", "A"), ("text", "B")]


@pytest.mark.asyncio
async def test_an_assistant_turn_with_nothing_to_replay_is_left_out(tmp_path: Path) -> None:
    """A stream cut off before any output stores an empty assistant turn; the user turns around it merge instead."""
    wire = Wire(sse_reply([{"type": "text", "text": "Done."}]))
    provider = route_provider("anthropic", tmp_path, wire)
    cut_off = Message(
        role="assistant",
        provider_data={"mindroom_model": {"id": "claude-sonnet-5-5", "provider": "Anthropic"}},
    )
    messages = [*_user("Summarize the report."), cut_off, Message(role="user", content="Try again.")]
    await claude_turn(provider, ModelRequest(messages=messages))
    turns = wire.body()["messages"]
    assert [turn["role"] for turn in turns] == ["user"]
    assert [block["text"] for block in turns[0]["content"]] == ["Summarize the report.", "Try again."]


@pytest.mark.parametrize(
    ("model_id", "prefill"),
    [
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", True),
        ("global.anthropic.claude-opus-4-6-v1:0", False),
        ("claude-sonnet-4@20250514", True),
        ("anthropic/claude-opus-4-6", False),
        ("claude-sonnet-4-6", False),
    ],
)
def test_prefill_support_reads_the_model_behind_route_decorated_ids(model_id: str, *, prefill: bool) -> None:
    """Bedrock's region prefix and version suffix, Vertex's snapshot, and a gateway's provider path hide no model."""
    assert supports_prefill(model_id) is prefill


@pytest.mark.asyncio
async def test_audio_inputs_are_not_sent(tmp_path: Path) -> None:
    """Claude takes images and documents; audio stays out of the request for the media fallback to describe."""
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider("anthropic", tmp_path, wire)
    audio = Audio(content=b"RIFF....WAVE", mime_type="audio/wav")
    await claude_turn(provider, ModelRequest(messages=[Message(role="user", content="Listen.", audio=[audio])]))
    assert [block["type"] for block in wire.body()["messages"][0]["content"]] == ["text"]


class _Answer(BaseModel):
    city: str


@pytest.mark.asyncio
async def test_direct_route_sends_strict_tools_and_schemas_under_the_structured_outputs_beta(tmp_path: Path) -> None:
    """Strict tools and a pydantic response format use the beta endpoint with its header."""
    wire = Wire(json_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    provider = route_provider("anthropic", tmp_path, wire)
    tools = [ToolSpec(name="pick", description="Pick.", parameters={"type": "object", "properties": {}}, strict=True)]
    completed = await claude_turn(
        provider,
        ModelRequest(messages=_user(), tools=tools, output_schema=_Answer, stream=False),
    )
    request = wire.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert request.headers["anthropic-beta"] == "structured-outputs-2025-11-13"
    body = wire.body()
    assert "stream" not in body
    assert body["tools"][0]["strict"] is True
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert completed.message.content == '{"city": "Paris"}'
    assert completed.stop_reason == "end"


@pytest.mark.asyncio
async def test_authored_output_config_and_stop_sequences_reach_the_request(tmp_path: Path) -> None:
    """``output_config`` and ``stop_sequences`` go out as authored; a structured answer adds its format beside them."""
    wire = Wire(json_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    authored = {"output_config": {"effort": "low"}, "stop_sequences": ["</answer>"]}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=authored)
    await claude_turn(provider, ModelRequest(messages=_user(), output_schema=_Answer, stream=False))
    body = wire.body()
    assert body["stop_sequences"] == ["</answer>"]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert provider.settings.output_config == {"effort": "low"}, "the format never leaks into the authored setting"


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured-output"])
async def test_documents_ask_for_citations_except_under_structured_output(tmp_path: Path, *, structured: bool) -> None:
    """The API rejects citations beside an output format, so a structured answer sends its documents without them."""
    wire = Wire(json_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    provider = route_provider("anthropic", tmp_path, wire)
    document = File(content=b"Paris is in France.", mime_type="text/plain")
    messages = [Message(role="user", content="Where is Paris?", files=[document])]
    request = ModelRequest(messages=messages, output_schema=_Answer if structured else None, stream=False)

    await claude_turn(provider, request)

    body = wire.body()
    (block,) = [block for block in body["messages"][-1]["content"] if block["type"] == "document"]
    assert ("citations" in block, "output_config" in body) == (not structured, structured)
    if not structured:
        assert block["citations"] == {"enabled": True}


@pytest.mark.asyncio
async def test_vertex_route_strips_strict_and_answers_in_json_mode(tmp_path: Path) -> None:
    """Vertex rejects provider-level strict fields and has no structured output, so the schema stays out."""
    wire = Wire(json_reply([{"type": "text", "text": "{}"}]))
    provider = route_provider("vertexai_claude", tmp_path, wire)
    tools = [ToolSpec(name="pick", description="Pick.", parameters={"type": "object", "properties": {}}, strict=True)]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools, output_schema=_Answer, stream=False))
    request = wire.requests[0]
    assert request.url.path.endswith(":rawPredict")
    assert "anthropic-beta" not in request.headers
    body = wire.body()
    assert "strict" not in body["tools"][0]
    assert "output_config" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "strict_sent", "url_suffix", "beta_header"),
    [
        ("anthropic", True, "/v1/messages?beta=true", "structured-outputs-2025-11-13"),
        ("vertexai_claude", False, ":streamRawPredict", None),
        ("bedrock_claude", True, "/v1/messages?beta=true", None),
    ],
)
async def test_tool_definitions_carry_only_the_fields_agno_sent(
    tmp_path: Path,
    route: str,
    strict_sent: bool,
    url_suffix: str,
    beta_header: str | None,
) -> None:
    """An unset description goes out empty, ``strict`` only when True on a route that takes it, approval flags never.

    The direct API adds the structured-outputs beta header for a strict tool; Bedrock sends the strict tool
    through the beta endpoint without one.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider(route, tmp_path, wire, extra_kwargs={"cache_system_prompt": False})
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    tools = [
        ToolSpec(name="unset", description=None, parameters=schema, strict=None),
        ToolSpec(
            name="gated",
            description="Gated.",
            parameters=schema,
            strict=True,
            requires_confirmation=True,
            external_execution=True,
            approval_type="mindroom_policy",
        ),
        ToolSpec(
            name="plain",
            description="Plain.",
            parameters=schema,
            strict=False,
            requires_confirmation=False,
            external_execution=False,
        ),
    ]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools))
    input_schema = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": ""}},
        "required": [],
        "additionalProperties": False,
    }
    assert wire.body()["tools"] == [
        {"name": "gated", "description": "Gated.", "input_schema": input_schema}
        | ({"strict": True} if strict_sent else {}),
        {"name": "plain", "description": "Plain.", "input_schema": input_schema},
        {"name": "unset", "description": "", "input_schema": input_schema},
    ]
    request = wire.requests[0]
    assert str(request.url).endswith(url_suffix)
    assert request.headers.get("anthropic-beta") == beta_header


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_choice", "expected"),
    [("none", {"type": "none"}), (None, {"type": "any"})],
)
async def test_request_tool_choice_replaces_an_authored_one(
    tmp_path: Path,
    tool_choice: Literal["none"] | None,
    expected: dict[str, str],
) -> None:
    """A decision's ``none`` wins over ``request_params``, including the ``extra_body`` copy the SDK merges last."""
    wire = Wire(json_reply([{"type": "text", "text": "{}"}]))
    authored = {"request_params": {"tool_choice": {"type": "auto"}, "extra_body": {"tool_choice": {"type": "any"}}}}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=authored)
    tools = [ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object", "properties": {}})]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools, tool_choice=tool_choice, stream=False))
    assert wire.body()["tool_choice"] == expected


@pytest.mark.asyncio
async def test_tool_choice_is_left_out_of_a_request_without_tools(tmp_path: Path) -> None:
    """The API rejects a tool choice without tools, so a decision request that offers none sends no choice."""
    wire = Wire(json_reply([{"type": "text", "text": "{}"}]))
    await claude_turn(
        route_provider("anthropic", tmp_path, wire),
        ModelRequest(messages=_user(), tool_choice="none", stream=False),
    )
    assert "tool_choice" not in wire.body()


@pytest.mark.asyncio
async def test_native_compaction_sends_the_edit_and_replays_the_recorded_checkpoint(tmp_path: Path) -> None:
    """The request asks Claude to compact; the checkpoint it returns replaces the earlier prefix next time."""
    checkpoint = {"type": "compaction", "content": "Summary so far.", "encrypted_content": "opaque"}
    usage = {
        **CLAUDE_USAGE,
        "iterations": [
            {
                "type": "compaction",
                "input_tokens": 60000,
                "output_tokens": 900,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            {
                "type": "message",
                "model": "claude-sonnet-5-5",
                "input_tokens": 40,
                "output_tokens": 9,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 3,
            },
        ],
    }
    wire = Wire(
        json_reply([checkpoint, {"type": "text", "text": "Done."}], usage=usage),
        sse_reply([{"type": "text", "text": "Next."}]),
    )
    provider = route_provider("anthropic", tmp_path, wire)
    replay = provider.native_compaction_settings(threshold=60000, history_generation="gen-1", allow_authored=False)
    assert replay is not None
    assert replay.threshold == 60000
    assert provider.native_compaction_settings(threshold=1000, history_generation="", allow_authored=False) is None, (
        "Claude needs at least 50,000 input tokens"
    )
    assert (
        provider.native_compaction_settings(threshold=60000, history_generation="gen-2", allow_authored=False) != replay
    )

    first = await claude_turn(
        provider,
        ModelRequest(
            messages=_user("Long history."),
            stream=False,
            native_compaction=replay,
        ),
    )
    request = wire.requests[0]
    assert request.headers["anthropic-beta"] == "compact-2026-01-12"
    assert str(request.url).endswith("?beta=true")
    edit = wire.body(0)["context_management"]["edits"][0]
    assert (edit["type"], edit["trigger"], edit["pause_after_compaction"]) == (
        "compact_20260112",
        {"type": "input_tokens", "value": 60000},
        False,
    )
    state = (first.message.provider_data or {})["mindroom_native_compaction"]
    assert (state["route"], state["threshold"], state["checkpoint_prefix"]) == (replay.route, 60000, True)
    assert state["items"] == [checkpoint, {"type": "text", "text": "Done."}]
    metrics = first.message.metrics
    assert (metrics.tokens.input_tokens, metrics.tokens.output_tokens, metrics.tokens.total_tokens) == (
        60040,
        909,
        60949,
    )
    assert metrics.provider_metrics == {
        "context_usage": {"input_tokens": 40, "cache_read_tokens": 5, "cache_write_tokens": 3},
    }

    messages = [*_user("Long history."), first.message, Message(role="user", content="Continue.")]
    second = await claude_turn(provider, ModelRequest(messages=messages, native_compaction=replay))
    turns = wire.body(1)["messages"]
    assert [turn["role"] for turn in turns] == ["assistant", "user"], "the checkpoint replaces the earlier user prefix"
    assert turns[0]["content"][0] == checkpoint
    assert (second.message.provider_data or {})["mindroom_native_compaction"] == {
        "route": replay.route,
        "threshold": 60000,
        "checkpoint_prefix": True,
    }
    # The canonical history is untouched: the stored message still starts with its compaction block.
    assert (first.message.provider_data or {})["content_blocks"][0]["type"] == "compaction"
    assert messages[0].content == "Be brief."


@pytest.mark.asyncio
async def test_authored_compaction_edits_keep_their_policy_and_cannot_pause(tmp_path: Path) -> None:
    """A model that authored its own compaction edit replays checkpoints without MindRoom's trigger."""
    authored = {"edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 80000}}]}
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": authored})
    assert provider.capabilities.native_compaction is False
    assert provider.native_compaction_settings(threshold=60000, history_generation="", allow_authored=False) is None
    replay = provider.native_compaction_settings(threshold=60000, history_generation="", allow_authored=True)
    assert replay is not None
    assert replay.threshold is None
    await claude_turn(provider, ModelRequest(messages=_user(), native_compaction=replay))
    assert wire.body()["context_management"] == authored
    assert wire.requests[0].headers["anthropic-beta"] == "compact-2026-01-12"

    paused = {"edits": [{"type": "compact_20260112", "pause_after_compaction": True}]}
    pausing = route_provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": paused})
    with pytest.raises(ValueError, match="pause_after_compaction"):
        await claude_turn(pausing, ModelRequest(messages=_user()))


def test_bedrock_replays_no_native_checkpoints_even_with_an_authored_compaction_edit(tmp_path: Path) -> None:
    """Bedrock checkpoints have no endpoint to bind to, so neither MindRoom's trigger nor the authored policy replays."""
    authored = {"edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 80000}}]}
    provider = route_provider(
        "bedrock_claude",
        tmp_path,
        Wire(json_reply([])),
        extra_kwargs={"context_management": authored},
    )
    assert provider.capabilities.native_compaction is False
    assert provider.native_compaction_settings(threshold=60000, history_generation="", allow_authored=True) is None
    assert provider.native_compaction_settings(threshold=None, history_generation="", allow_authored=True) is None


@pytest.mark.asyncio
async def test_an_authored_context_edit_records_what_the_response_applied(tmp_path: Path) -> None:
    """A model authoring context editing keeps the edits each response applied with its message."""
    applied = {
        "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3, "cleared_input_tokens": 12000}],
    }
    wire = Wire(
        httpx.Response(200, json=claude_message([{"type": "text", "text": "Ok."}]) | {"context_management": applied}),
    )
    authored = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": authored})
    completed = await claude_turn(provider, ModelRequest(messages=_user(), stream=False))
    assert str(wire.requests[0].url).endswith("?beta=true")
    assert wire.body()["context_management"] == authored
    assert (completed.message.provider_data or {})["context_management"] == applied


@pytest.mark.asyncio
async def test_replay_drops_unsigned_thinking_from_a_cut_off_response(tmp_path: Path) -> None:
    """A stream that stopped mid-block leaves an unsigned thinking block, which the API rejects on replay."""
    signed = {"type": "thinking", "thinking": "", "signature": "complete"}
    unsigned = {"type": "thinking", "thinking": "", "signature": ""}
    tool_use = {"type": "tool_use", "id": "toolu_save", "name": "save_file", "input": {"file_name": "notes.md"}}
    unfinished = {"type": "tool_use", "id": "toolu_shell", "name": "run_shell_command", "input": {}}
    stored = [signed, tool_use, unsigned, unfinished]
    wire = Wire(sse_reply([{"type": "text", "text": "Saved."}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    messages = [
        Message(role="user", content="Save the notes."),
        Message(
            role="assistant",
            content="",
            provider_data={"content_blocks": [dict(block) for block in stored], "signature": "complete"},
            tool_calls=[{"id": "toolu_save", "type": "function", "function": {"name": "save_file", "arguments": "{}"}}],
        ),
        Message(role="tool", tool_call_id="toolu_save", content="saved"),
    ]
    await claude_turn(provider, ModelRequest(messages=messages))
    assert wire.body()["messages"][1]["content"] == [signed, tool_use]
    assert (messages[1].provider_data or {})["content_blocks"] == stored


@pytest.mark.asyncio
async def test_bedrock_session_credentials_are_resolved_for_every_request(tmp_path: Path) -> None:
    """A boto3 session re-resolves rotating credentials before each request, in the session's region."""

    class _Frozen:
        access_key = "AKIA-rotated"
        secret_key = "secret"  # noqa: S105 - synthetic test credential
        token = "session-token"  # noqa: S105 - synthetic test credential

    class _Credentials:
        def get_frozen_credentials(self) -> _Frozen:
            return _Frozen()

    class _Session:
        region_name = "eu-west-1"
        resolutions = 0

        def get_credentials(self) -> _Credentials:
            self.resolutions += 1
            return _Credentials()

    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    config = {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5-5",
        "extra_kwargs": {"session": _Session()},
    }
    provider = provider_for(config, tmp_path, wire.client)
    await claude_turn(provider, ModelRequest(messages=_user()))
    await claude_turn(provider, ModelRequest(messages=_user()))
    session = provider.settings.session
    assert isinstance(session, _Session)
    assert session.resolutions == 2
    assert all(request.url.host == "bedrock-mantle.eu-west-1.api.aws" for request in wire.requests)


@pytest.mark.asyncio
async def test_a_bedrock_session_without_credentials_fails_the_request_with_the_fix(tmp_path: Path) -> None:
    """A boto3 session that resolves no credentials stops the request and says where AWS looks for them."""

    class _Session:
        region_name = "eu-west-1"

        def get_credentials(self) -> None:
            return None

    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    config = {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5-5",
        "extra_kwargs": {"session": _Session()},
    }
    provider = provider_for(config, tmp_path, wire.client)
    with pytest.raises(ValueError, match=r"boto3 session has no credentials\. Check your AWS configuration"):
        await claude_turn(provider, ModelRequest(messages=_user()))
    assert wire.requests == []


def test_bedrock_static_keys_win_over_a_profile_and_the_ambient_chain_needs_no_region(tmp_path: Path) -> None:
    """Authored keys beat an authored profile; without keys or a region, a boto3 session resolves both itself."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})
    keys = {"aws_access_key": "AKIA-static", "aws_secret_key": "static-secret", "aws_profile": "dev"}
    with isolated_provider_environment():
        static = build_provider(
            ModelConfig(provider="bedrock_claude", id="anthropic.claude-sonnet-5-5", extra_kwargs=keys),
            runtime_paths,
        )
        ambient = build_provider(
            ModelConfig(provider="bedrock_claude", id="anthropic.claude-sonnet-5-5"),
            runtime_paths,
        )
    assert (static.settings.aws_access_key, static.settings.aws_profile, static.settings.session) == (
        "AKIA-static",
        None,
        None,
    )
    assert static.settings.aws_region is None
    assert isinstance(ambient.settings.session, boto3.session.Session)
    assert (ambient.settings.aws_access_key, ambient.settings.aws_region) == (None, None)


@pytest.mark.asyncio
async def test_build_provider_resolves_route_credentials_from_the_runtime_env(tmp_path: Path) -> None:
    """Project, region, and AWS credentials come from the runtime env when the config leaves them out."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    env = {
        "ANTHROPIC_VERTEX_PROJECT_ID": "env-project",
        "CLOUD_ML_REGION": "europe-west1",
        "AWS_ACCESS_KEY_ID": "env-access",
        "AWS_SECRET_ACCESS_KEY": "env-secret",
        "AWS_REGION": "us-west-2",
        "ANTHROPIC_VERTEX_BASE_URL": "https://vertex-gateway.example/v1",
    }
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    with isolated_provider_environment():
        direct = build_provider(
            ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="config-key"),
            runtime_paths,
        )
        await claude_turn(replace(direct, http_client=wire.client), ModelRequest(messages=_user()))
        assert wire.requests[-1].headers["x-api-key"] == "config-key"

        bedrock = build_provider(
            ModelConfig(provider="bedrock_claude", id="anthropic.claude-sonnet-5-5"),
            runtime_paths,
        )
        await claude_turn(replace(bedrock, http_client=wire.client), ModelRequest(messages=_user()))
        assert wire.requests[-1].url.host == "bedrock-mantle.us-west-2.api.aws"
        assert (bedrock.settings.aws_access_key, bedrock.settings.session) == ("env-access", None)

        vertex = build_provider(
            ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5", api_key="ignored"),
            runtime_paths,
        )
        assert (vertex.settings.project_id, vertex.settings.region, vertex.settings.api_key) == (
            "env-project",
            "europe-west1",
            None,
        )
        assert vertex.settings.base_url == "https://vertex-gateway.example/v1"
        assert (
            provider_for(
                {"provider": "vertexai_claude", "id": "claude-sonnet-5-5"},
                tmp_path,
                wire.client,
            ).settings.region
            is None
        )


@pytest.mark.asyncio
async def test_client_params_override_route_settings_but_not_headers_or_the_http_client(tmp_path: Path) -> None:
    """``client_params`` may override any SDK client argument, while ``default_headers`` and the HTTP client win."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    direct = route_provider(
        "anthropic",
        tmp_path,
        wire,
        extra_kwargs={
            "default_headers": {"x-mindroom": "authored"},
            "client_params": {
                "api_key": "params-key",
                "base_url": "https://gateway.example/anthropic",
                "default_headers": {"x-mindroom": "params"},
                "http_client": object(),  # never used: requests go through the adapter's shared client
            },
        },
    )
    await claude_turn(direct, ModelRequest(messages=_user()))
    request = wire.requests[-1]
    assert str(request.url) == "https://gateway.example/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "params-key"
    assert request.headers["x-mindroom"] == "authored"

    vertex = route_provider(
        "vertexai_claude",
        tmp_path,
        wire,
        extra_kwargs={
            "client_params": {"base_url": "https://vertex.example/v1", "project_id": "params-project", "region": "eu"},
        },
    )
    await claude_turn(vertex, ModelRequest(messages=_user()))
    assert str(wire.requests[-1].url) == (
        "https://vertex.example/v1/projects/params-project/locations/eu/publishers/anthropic/models/"
        "claude-sonnet-5-5:streamRawPredict"
    )

    bedrock = route_provider(
        "bedrock_claude",
        tmp_path,
        wire,
        extra_kwargs={"client_params": {"aws_region": "eu-central-1"}},
    )
    await claude_turn(bedrock, ModelRequest(messages=_user()))
    assert wire.requests[-1].url.host == "bedrock-mantle.eu-central-1.api.aws"


@pytest.mark.asyncio
async def test_requests_get_an_hour_unless_the_model_authors_no_timeout(tmp_path: Path) -> None:
    """The one-hour default lets long non-streamed answers finish; an authored null keeps the SDK's own limits."""
    wire = Wire(json_reply([{"type": "text", "text": "Hi."}]))
    request = ModelRequest(messages=_user(), stream=False)
    await claude_turn(route_provider("anthropic", tmp_path, wire), request)
    await claude_turn(route_provider("anthropic", tmp_path, wire, extra_kwargs={"timeout": None}), request)
    assert [sent.extensions["timeout"] for sent in wire.requests] == [
        httpx.Timeout(3600.0).as_dict(),
        anthropic.DEFAULT_TIMEOUT.as_dict(),
    ]


@pytest.mark.asyncio
async def test_vertex_discovers_ambient_google_credentials_once_per_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without configured credentials, ADC discovery runs once; every request reuses them and their project."""
    discoveries = 0

    def default(scopes: list[str]) -> tuple[Credentials, str]:
        nonlocal discoveries
        discoveries += 1
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return Credentials(token="ambient-token"), "adc-project"  # noqa: S106 - synthetic SDK credential

    monkeypatch.setattr(google.auth, "default", default)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    with isolated_provider_environment():
        built = build_provider(
            ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5", extra_kwargs={"region": "us-east5"}),
            runtime_paths,
        )
    assert built.google_credentials.credentials is None, "discovery waits for the first request"
    provider = replace(built, http_client=wire.client)
    await claude_turn(provider, ModelRequest(messages=_user()))
    await claude_turn(provider, ModelRequest(messages=_user()))
    assert discoveries == 1
    assert [request.headers["authorization"] for request in wire.requests] == ["Bearer ambient-token"] * 2
    assert all(request.url.path.startswith("/v1/projects/adc-project/locations/us-east5/") for request in wire.requests)


# What google-auth's requests transport reports when the token endpoint cannot be reached.
_REQUESTS_OAUTH_HOST_FAILURE = (
    "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded with url: /token "
    '(Caused by NameResolutionError("<urllib3.connection.HTTPSConnection object at 0x7f0>: '
    "Failed to resolve 'oauth2.googleapis.com' ([Errno -3] Temporary failure in name resolution)\"))"
)
_UNAVAILABLE_FINAL_MESSAGE = "⚠️ Model provider temporarily unavailable after automatic retries"


class _RefreshFailingCredentials(Credentials):
    """Credentials whose token refresh fails the way google-auth reports it."""

    def __init__(self, error: Exception) -> None:
        super().__init__(token=None)
        self.error = error

    def refresh(self, request: object) -> None:  # noqa: ARG002 - google-auth passes its transport
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected", "status_code", "final_message"),
    [
        (
            google.auth.exceptions.TransportError("token endpoint unreachable"),
            ProviderUnavailable,
            None,
            _UNAVAILABLE_FINAL_MESSAGE,
        ),
        (
            google.auth.exceptions.TransportError(_REQUESTS_OAUTH_HOST_FAILURE),
            ProviderUnavailable,
            None,
            _UNAVAILABLE_FINAL_MESSAGE,
        ),
        (
            google.auth.exceptions.RefreshError("temporarily_unavailable", retryable=True),
            ProviderUnavailable,
            None,
            _UNAVAILABLE_FINAL_MESSAGE,
        ),
        (
            google.auth.exceptions.RefreshError("invalid_grant: Invalid JWT Signature."),
            AuthenticationFailed,
            401,
            "❌ Authentication failed",
        ),
    ],
    ids=["transport", "transport-requests-message", "retryable-refresh", "rejected-credentials"],
)
async def test_vertex_token_refresh_failures_map_to_the_shared_provider_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: type[ProviderError],
    status_code: int | None,
    final_message: str,
) -> None:
    """A network failure while minting the Vertex token is retryable and ends as temporary unavailability.

    Rejected credentials end as an authentication failure.
    """
    monkeypatch.setattr(google.auth, "default", lambda scopes: (_RefreshFailingCredentials(failure), "adc-project"))  # noqa: ARG005
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    with isolated_provider_environment():
        built = build_provider(
            ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5", extra_kwargs={"region": "us-east5"}),
            runtime_paths,
        )
    provider = replace(built, http_client=wire.client)
    with pytest.raises(expected) as raised:
        await claude_turn(provider, ModelRequest(messages=_user()))
    assert type(raised.value) is expected
    assert raised.value.status_code == status_code
    assert raised.value.__cause__ is failure
    assert wire.requests == []
    assert get_user_friendly_error_message(raised.value).startswith(final_message)


@pytest.mark.asyncio
async def test_url_media_is_fetched_inline_except_pdf_documents(tmp_path: Path) -> None:
    """URL images and non-PDF URL documents are fetched and sent inline; Claude reads a PDF from its URL itself."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host != "media.example":
            return sse_reply([{"type": "text", "text": "Seen."}])
        if request.url.path == "/photo":
            return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
        if request.url.path == "/pic.png":
            return httpx.Response(200, content=_PNG, headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, content=b"# Notes\n", headers={"content-type": "text/markdown; charset=utf-8"})

    wire = Wire(respond)
    provider = route_provider("anthropic", tmp_path, wire)
    message = Message(
        role="user",
        content="Look.",
        images=[Image(url="https://media.example/photo"), Image(url="https://media.example/pic.png")],
        files=[
            File(url="https://media.example/notes", mime_type="text/markdown"),
            File(url="https://media.example/report.pdf", mime_type="application/pdf"),
        ],
    )
    await claude_turn(provider, ModelRequest(messages=[message]))
    blocks = wire.body()["messages"][0]["content"]
    assert [block["type"] for block in blocks] == ["text", "image", "image", "document", "document"]
    png = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(_PNG).decode()}
    assert blocks[1]["source"] == png
    assert blocks[2]["source"] == png
    assert blocks[3]["source"] == {"type": "text", "media_type": "text/plain", "data": "# Notes\n"}
    assert blocks[4]["source"] == {"type": "url", "url": "https://media.example/report.pdf"}
    assert [str(request.url) for request in wire.requests[:-1]] == [
        "https://media.example/photo",
        "https://media.example/pic.png",
        "https://media.example/notes",
    ]
    assert message.images is not None
    assert all(image.url is not None for image in message.images), "the request's inlined copies leave the input alone"


@pytest.mark.asyncio
async def test_unreadable_url_media_is_dropped_and_the_request_still_runs(tmp_path: Path) -> None:
    """An expired or unreachable URL drops its media with a warning; the rest of the message is still sent."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host != "media.example":
            return sse_reply([{"type": "text", "text": "Ok."}])
        if request.url.path == "/gone":
            return httpx.Response(404)
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    wire = Wire(respond)
    provider = route_provider("anthropic", tmp_path, wire)
    message = Message(
        role="user",
        content="Look.",
        images=[Image(url="https://media.example/gone")],
        files=[File(url="https://media.example/down", mime_type="text/plain")],
    )
    completed = await claude_turn(provider, ModelRequest(messages=[message]))
    assert completed.message.content == "Ok."
    blocks = wire.body()["messages"][0]["content"]
    assert [(block["type"], block["text"]) for block in blocks] == [("text", "Look.")]


@pytest.mark.asyncio
async def test_url_images_are_inlined_in_every_user_turn_beside_inline_images(tmp_path: Path) -> None:
    """Each user turn's URL images are fetched where they sat, and images already carried inline stay as they are."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host != "media.example":
            return sse_reply([{"type": "text", "text": "Seen."}])
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    wire = Wire(respond)
    inline = Image(content=_GIF, mime_type="image/gif")
    messages = [
        Message(role="user", content="First.", images=[inline, Image(url="https://media.example/one")]),
        Message(role="assistant", content="Seen."),
        Message(role="user", content="Second.", images=[Image(url="https://media.example/two")]),
    ]
    await claude_turn(route_provider("anthropic", tmp_path, wire), ModelRequest(messages=messages))
    first, _, second = wire.body()["messages"]
    gif = {"type": "base64", "media_type": "image/gif", "data": base64.b64encode(_GIF).decode()}
    png = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(_PNG).decode()}
    assert [block.get("source") for block in first["content"]] == [None, gif, png]
    assert [block.get("source") for block in second["content"]] == [None, png]
    assert [request.url.path for request in wire.requests[:-1]] == ["/one", "/two"]


def _markers(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        found = [payload["cache_control"]] if "cache_control" in payload else []
        return found + [marker for value in payload.values() for marker in _markers(value)]
    if isinstance(payload, list):
        return [marker for item in payload for marker in _markers(item)]
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(ROUTE_CONFIGS))
@pytest.mark.parametrize("cache", ["extended", "short", "disabled"])
async def test_the_shared_instructions_stay_cacheable_when_the_session_context_changes(
    tmp_path: Path,
    route: str,
    cache: str,
) -> None:
    """The instructions before the session context keep one marked block, so a new date reuses the cached prefix."""
    settings = {"extended_cache_time": cache == "extended", "cache_system_prompt": cache != "disabled"}
    bodies = []
    for day in ("Monday", "Tuesday"):
        wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
        (tmp_path / day).mkdir()
        provider = route_provider(route, tmp_path / day, wire, extra_kwargs=settings)
        system = "Shared instructions.\n\n" + render_session_context(f"Current date: {day}")
        messages = [
            Message(role="system", content=system),
            Message(role="user", content="First request."),
            Message(role="assistant", content="First answer."),
            Message(role="user", content="Follow-up request."),
        ]
        await claude_turn(provider, ModelRequest(messages=messages))
        bodies.append(wire.body())

    monday, tuesday = bodies
    if cache == "disabled":
        assert _markers(monday) == []
        return
    cache_control = {"type": "ephemeral", **({"ttl": "1h"} if cache == "extended" else {})}
    assert monday["system"][0] == tuesday["system"][0]
    assert monday["system"][0] == {"type": "text", "text": "Shared instructions.\n\n", "cache_control": cache_control}
    assert "cache_control" not in monday["system"][1]
    assert monday["system"][1] != tuesday["system"][1]
    assert monday["messages"][-1]["content"][-1]["cache_control"] == cache_control
    assert len(_markers(monday)) <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authored_system",
    [
        "Authored system.",
        [{"type": "text", "text": "Authored system."}],
        [{"type": "text", "text": "Authored system.", "cache_control": {"type": "ephemeral"}}],
    ],
    ids=["string", "unmarked-blocks", "marked-blocks"],
)
async def test_a_raw_system_override_goes_as_authored_without_a_system_message(
    tmp_path: Path,
    authored_system: str | list[dict[str, Any]],
) -> None:
    """Without a system message, ``request_params.system`` reaches the API unchanged, its cache markers included."""
    wire = Wire(json_reply([{"type": "text", "text": "Hi."}]))
    settings = {"cache_system_prompt": True, "extended_cache_time": True, "request_params": {"system": authored_system}}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=settings)
    await claude_turn(provider, ModelRequest(messages=[Message(role="user", content="Hello")], stream=False))
    assert wire.body()["system"] == authored_system
