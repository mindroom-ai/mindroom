"""Behavior of the owned Claude adapter beyond the goldens: settings, events, stop reasons, errors, overrides, replay."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import anthropic
import boto3.session
import google.auth
import google.auth.exceptions
import httpx
import httpx2
import pytest
from google.oauth2.credentials import Credentials
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.llm.errors import (
    MISSING_API_KEY,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.media import Audio, File
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
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.anthropic.messages import build_provider
from mindroom.providers.anthropic.settings import parse_claude_settings
from mindroom.providers.anthropic.wire import checks_thinking_prefix, supports_prefill, supports_structured_outputs
from mindroom.providers.pipeline import retry as retry_stage
from tests.providers.anthropic_family_support import (
    CLAUDE_USAGE,
    ROUTE_CONFIGS,
    claude_events,
    claude_message,
    claude_turn,
    isolated_provider_environment,
    native_settings,
    provider_for,
    route_provider,
    sse_reply,
)
from tests.providers.golden_support import (
    Wire,
    claude_stream_events,
    hide_provider_environment,
    pipeline_provider,
    sse_response,
)

if TYPE_CHECKING:
    from pathlib import Path

# Sonnet 5.5, the model these tests default to, asks the Anthropic and Vertex routes to drop stale replayed thinking.
_THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
_DROP_STALE = {"prefix_mismatch_behavior": "drop_block"}


def _user(text: str = "Hello") -> list[Message]:
    return [Message(role="system", content="Be brief."), Message(role="user", content=text)]


def test_unknown_extra_kwargs_are_rejected_with_the_accepted_keys() -> None:
    """Dropped Agno features and another route's keys fail at construction and name what is accepted."""
    with pytest.raises(ValueError, match=r"skills.*Accepted keys: .*cache_system_prompt") as skills:
        parse_claude_settings("anthropic", "claude-sonnet-5-5", {"skills": [{"type": "anthropic", "skill_id": "pptx"}]})
    assert "mcp_servers" not in str(skills.value)
    with pytest.raises(ValueError, match="project_id"):
        parse_claude_settings("anthropic", "claude-sonnet-5-5", {"project_id": "p"})
    assert (
        parse_claude_settings("vertexai_claude", "claude-sonnet-5-5", {"project_id": "p", "region": "global"}).region
        == "global"
    )
    settings = parse_claude_settings(
        "bedrock_claude",
        "anthropic.claude-sonnet-5-5",
        {"aws_region": "us-east-1", "cache_system_prompt": False},
    )
    assert (settings.aws_region, settings.cache_system_prompt, settings.extended_cache_time) == (
        "us-east-1",
        False,
        True,
    )
    assert settings.max_tokens == 8192
    assert settings.timeout is None
    assert parse_claude_settings("anthropic", "claude-sonnet-5-5", {"timeout": 120.0}).timeout == 120.0


@pytest.mark.parametrize(
    ("route", "provider_label", "structured_output", "native_tool_search"),
    [
        ("anthropic", "Anthropic", "native", True),
        ("vertexai_claude", "VertexAI", "json_mode", True),
        ("bedrock_claude", "AwsBedrock", "json_mode", False),
    ],
)
def test_identity_and_capabilities_per_route(
    tmp_path: Path,
    route: str,
    provider_label: str,
    structured_output: str,
    native_tool_search: bool,
) -> None:
    """Identity strings stay what stored runs and usage rows carry, and capabilities replace class probes."""
    provider = route_provider(route, tmp_path, Wire(sse_reply([])))
    assert provider.identity.provider == provider_label
    capabilities = provider.capabilities
    assert capabilities.structured_output == structured_output
    assert capabilities.native_tool_search is native_tool_search
    assert capabilities.omitted_media == frozenset({"audio", "video"})
    assert capabilities.conservative_token_estimates is True
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
    tools = [
        ToolSpec(
            name="get_weather",
            description="Forecast.",
            parameters={"type": "object", "properties": {}},
            strict=None,
        ),
    ]
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
    assert message.metrics.context == TokenCounts(input_tokens=40, cache_read_tokens=5, cache_write_tokens=3)
    assert message.metrics.duration is not None
    assert message.metrics.time_to_first_token is not None
    assert message.content == "Looking it up."
    assert message.reasoning_content == "Check first."
    assert message.tool_calls == [events[3].call]
    assert message.provider_data == {
        "claude_blocks": [
            {"type": "thinking", "thinking": "Check first.", "signature": "sig"},
            {"type": "text", "text": "Looking it up."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "mindroom_model": {"id": "claude-sonnet-5-5", "provider": "Anthropic"},
        "mindroom_native_compaction": None,
    }
    assert wire.body()["thinking"] == {"type": "adaptive", "block_binding": _DROP_STALE}


@pytest.mark.asyncio
async def test_a_reply_stores_every_thinking_block_joined(tmp_path: Path) -> None:
    """A reply's text joins its text blocks, its reasoning its thinking blocks, and its redacted reasoning the rest."""
    blocks = [
        {"type": "thinking", "thinking": "First. ", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "redacted-1"},
        {"type": "text", "text": "Half "},
        {"type": "thinking", "thinking": "Second.", "signature": "sig-2"},
        {"type": "redacted_thinking", "data": "redacted-2"},
        {"type": "text", "text": "done."},
    ]
    provider = route_provider(
        "anthropic",
        tmp_path,
        Wire(sse_reply(blocks)),
        extra_kwargs={"thinking": {"type": "adaptive"}},
    )

    message = (await claude_turn(provider, ModelRequest(messages=_user()))).message

    assert (message.reasoning_content, message.redacted_reasoning_content) == ("First. Second.", "redacted-1redacted-2")
    assert message.content == "Half done."
    assert (message.provider_data or {})["claude_blocks"] == blocks


@pytest.mark.asyncio
async def test_a_thinking_block_without_text_stores_empty_reasoning(tmp_path: Path) -> None:
    """A signed thinking block whose text is empty stores empty reasoning, which tells it from a reply without one."""
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "Done."}]
    provider = route_provider(
        "anthropic",
        tmp_path,
        Wire(sse_reply(blocks)),
        extra_kwargs={"thinking": {"type": "adaptive"}},
    )

    message = (await claude_turn(provider, ModelRequest(messages=_user()))).message

    assert (message.content, message.reasoning_content) == ("Done.", "")


@pytest.mark.asyncio
async def test_a_drained_reply_keeps_what_a_streamed_reply_keeps(tmp_path: Path) -> None:
    """A caller that reads only the finished turn gets every thinking block and the time to its first output."""
    blocks = [
        {"type": "thinking", "thinking": "First. ", "signature": "sig-1"},
        {"type": "thinking", "thinking": "Second.", "signature": "sig-2"},
        {"type": "text", "text": "Done."},
    ]
    provider = route_provider(
        "anthropic",
        tmp_path,
        Wire(sse_reply(blocks)),
        extra_kwargs={"thinking": {"type": "adaptive"}},
    )

    with isolated_provider_environment():
        message = (await complete(provider, ModelRequest(messages=_user()))).message

    assert (message.content, message.reasoning_content) == ("Done.", "First. Second.")
    assert message.metrics.time_to_first_token is not None


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
async def test_usage_keeps_the_token_counts_and_context_but_not_the_service_tier_or_server_tool_requests(
    tmp_path: Path,
) -> None:
    """The direct API reports its service tier, and a web search its request count, which nothing reads."""
    usage = {**CLAUDE_USAGE, "service_tier": "standard", "server_tool_use": {"web_search_requests": 2}}
    wire = Wire(sse_reply([{"type": "text", "text": "Sunny."}], usage=usage))
    completed = await claude_turn(
        route_provider("anthropic", tmp_path, wire),
        ModelRequest(messages=_user()),
    )
    tokens = TokenCounts(input_tokens=40, output_tokens=9, total_tokens=49, cache_read_tokens=5, cache_write_tokens=3)
    assert replace(completed.message.metrics, duration=None, time_to_first_token=None) == MessageMetrics(
        tokens=tokens,
        context=TokenCounts(input_tokens=40, cache_read_tokens=5, cache_write_tokens=3),
    )


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


@pytest.mark.asyncio
async def test_block_types_the_sdk_does_not_know_are_kept_for_replay(tmp_path: Path) -> None:
    """A newer block type, which the SDK builds as a text block with its own ``type``, is kept, not read as text."""
    unknown = {"type": "future_block", "payload": "p"}
    text = {"type": "text", "text": "Hi."}
    reply = sse_reply([{**unknown, "deltas": [{"type": "future_delta", "payload": "q"}]}, dict(text)])
    events = await claude_events(route_provider("anthropic", tmp_path, Wire(reply)), ModelRequest(messages=_user()))
    outputs = [event for event in events if isinstance(event, (TextDelta, ReasoningDelta, ToolCallReady))]
    assert outputs == [TextDelta("Hi.")]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.message.content == "Hi."
    assert (completed.message.provider_data or {})["claude_blocks"] == [unknown, text]


@pytest.mark.asyncio
async def test_refusal_completes_with_the_refusal_stop_reason(tmp_path: Path) -> None:
    """A safeguard refusal is a completed turn the loop recognizes by its stop reason, not an adapter error."""
    wire = Wire(sse_reply([], stop_reason="refusal"))
    provider = route_provider("anthropic", tmp_path, wire)
    completed = await claude_turn(provider, ModelRequest(messages=_user()))
    assert completed.stop_reason == "refusal"
    assert completed.message.content is None
    # Every Claude turn stores its blocks, so a reply without any still reads as one with nothing to replay.
    assert (completed.message.provider_data or {})["claude_blocks"] == []
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
    wire = Wire(httpx.Response(status, json={"type": "error", "error": body}, headers={"retry-after": "3"}))
    provider = route_provider("anthropic", tmp_path, wire)
    with pytest.raises(expected) as raised:
        await claude_turn(provider, ModelRequest(messages=_user()))
    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert raised.value.provider == "Anthropic"
    assert raised.value.retry_after_seconds == 3.0
    assert body["message"] in str(raised.value)
    assert len(wire.requests) == 1, "the SDK client never retries; the pipeline alone does"


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
async def test_a_direct_route_without_any_credential_fails_as_a_missing_key(tmp_path: Path) -> None:
    """With no key or token anywhere the SDK looks, the request fails unsent and the reply says how to connect one."""
    wire = Wire(sse_reply([{"type": "text", "text": "unreachable"}]))

    with isolated_provider_environment():
        provider = provider_for({"provider": "anthropic", "id": "claude-sonnet-5-5"}, tmp_path, wire.client)
        with pytest.raises(AuthenticationFailed) as raised:
            await claude_turn(provider, ModelRequest(messages=_user()))

    assert (raised.value.code, raised.value.provider, wire.requests) == (MISSING_API_KEY, "Anthropic", [])
    assert "Connect your AI provider" in get_user_friendly_error_message(raised.value)


@pytest.mark.asyncio
async def test_transport_failures_are_retryable_provider_unavailable(tmp_path: Path) -> None:
    """A connection that never reaches the provider is retryable."""

    def fail(request: httpx2.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx2.ConnectError(msg, request=request)

    provider = route_provider("anthropic", tmp_path, Wire(fail))
    with pytest.raises(ProviderUnavailable) as raised:
        await claude_turn(provider, ModelRequest(messages=_user()))
    assert raised.value.retryable is True
    assert raised.value.status_code is None


@pytest.mark.asyncio
async def test_summary_overrides_drop_thinking_cache_markers_and_apply_sampling(tmp_path: Path) -> None:
    """One-off summary requests send no thinking setting and no cache writes, with their own limits."""
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
    overrides = RequestOverrides(temperature=0.1, prompt_cache=False, omit_thinking_setting=True)
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
@pytest.mark.parametrize(
    ("route", "model_id"),
    [
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic", "claude-haiku-5-5"),
        ("bedrock_claude", "anthropic.claude-haiku-5-5"),
        ("vertexai_claude", "claude-haiku-5-5"),
    ],
)
async def test_default_sampling_models_lose_sampling_controls_everywhere(
    tmp_path: Path,
    route: str,
    model_id: str,
) -> None:
    """The newest Claude generations reject non-default sampling, so authored and overriding values are dropped."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider(
        route,
        tmp_path,
        wire,
        id=model_id,
        extra_kwargs={"temperature": 0.7, "top_p": 0.9, "request_params": {"extra_body": {"top_k": 3}}},
    )
    await claude_turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(temperature=0.2)))
    body = wire.body()
    assert not {"temperature", "top_p", "top_k"} & body.keys()
    # The request ends with the conversation's own user turn and adds none: these models reject prefill.
    assert body["messages"][-1]["content"][0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_vertex_keeps_an_older_claude_s_authored_sampling_and_ignores_the_override(tmp_path: Path) -> None:
    """Vertex leaves overriding sampling to the provider, but only the newest generations lose the authored values."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider(
        "vertexai_claude",
        tmp_path,
        wire,
        id="claude-sonnet-4-5",
        extra_kwargs={"temperature": 0.7, "top_k": 3},
    )
    await claude_turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(temperature=0.2)))
    body = wire.body()
    assert (body["temperature"], body["top_k"]) == (0.7, 3)


@pytest.mark.asyncio
async def test_history_ending_with_an_assistant_turn_gets_a_trailing_user_turn(tmp_path: Path) -> None:
    """Models without prefill, and any model with thinking on, need the request to end with a user turn."""
    wire = Wire(sse_reply([{"type": "text", "text": "More."}]))
    provider = route_provider("anthropic", tmp_path, wire)
    stored = {
        "claude_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "A"}],
    }
    messages = [
        *_user(),
        Message(role="assistant", content="A", reasoning_content="t", provider_data=stored),
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


@pytest.mark.parametrize(
    ("model_id", "structured"),
    [
        ("anthropic/claude-3-5-haiku-20241022", False),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", False),
        ("claude-sonnet-4@20250514", False),
        ("anthropic/claude-opus-4-6", True),
        ("claude-sonnet-5-5", True),
    ],
)
def test_structured_output_support_reads_the_model_behind_route_decorated_ids(
    model_id: str,
    *,
    structured: bool,
) -> None:
    """A gateway path, Bedrock region, or Vertex snapshot does not make an older Claude model count as supporting it."""
    assert supports_structured_outputs(model_id) is structured


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
    wire = Wire(sse_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    provider = route_provider("anthropic", tmp_path, wire)
    tools = [
        ToolSpec(
            name="pick",
            description="Pick.",
            parameters={"type": "object", "properties": {}},
            strict=True,
        ),
    ]
    completed = await claude_turn(
        provider,
        ModelRequest(messages=_user(), tools=tools, output_schema=_Answer),
    )
    request = wire.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert request.headers["anthropic-beta"] == f"structured-outputs-2025-11-13,{_THINKING_BINDING_BETA}"
    body = wire.body()
    assert body["stream"] is True
    assert body["tools"][0]["strict"] is True
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert completed.message.content == '{"city": "Paris"}'
    assert completed.stop_reason == "end"


@pytest.mark.asyncio
async def test_authored_output_config_and_stop_sequences_reach_the_request(tmp_path: Path) -> None:
    """``output_config`` and ``stop_sequences`` go out as authored; a structured answer adds its format beside them."""
    wire = Wire(sse_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    authored = {"output_config": {"effort": "low"}, "stop_sequences": ["</answer>"]}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=authored)
    await claude_turn(provider, ModelRequest(messages=_user(), output_schema=_Answer))
    body = wire.body()
    assert body["stop_sequences"] == ["</answer>"]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert provider.settings.output_config == {"effort": "low"}, "the format never leaks into the authored setting"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "model_id", "extra_kwargs", "max_tokens"),
    [
        ("anthropic", "claude-opus-5-5", {}, 128_000),
        ("anthropic", "claude-sonnet-4-6", {}, 128_000),
        ("anthropic", "claude-haiku-4-5-20251001", {}, 64_000),
        ("anthropic", "claude-3-7-sonnet-20250219", {}, 8192),
        ("vertexai_claude", "claude-opus-5-5", {}, 8192),
        ("bedrock_claude", "anthropic.claude-opus-5-5", {}, 8192),
        ("anthropic", "claude-opus-5-5", {"max_tokens": 2000}, 2000),
        ("anthropic", "claude-opus-5-5", {"request_params": {"max_tokens": 3000}}, 3000),
        ("bedrock_claude", "anthropic.claude-opus-5-5", {"max_tokens": 32_000}, 32_000),
    ],
    ids=[
        "direct-claude-5",
        "direct-sonnet-4-6",
        "direct-haiku-4-5",
        "direct-unlisted-model",
        "vertex",
        "bedrock",
        "authored",
        "authored-request-params",
        "authored-bedrock",
    ],
)
async def test_a_reply_may_reach_the_models_maximum_output_only_on_the_direct_api(
    tmp_path: Path,
    route: str,
    model_id: str,
    extra_kwargs: dict[str, Any],
    max_tokens: int,
) -> None:
    """Vertex and Bedrock count the requested output against their quotas up front, so they keep the 8,192 cap.

    An authored ``max_tokens`` wins on every route, and a model the direct route does not list keeps 8,192.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "ok"}]))
    provider = route_provider(route, tmp_path, wire, id=model_id, extra_kwargs=extra_kwargs)
    await claude_turn(provider, ModelRequest(messages=_user()))
    assert wire.body()["max_tokens"] == max_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured-output"])
async def test_documents_ask_for_no_citations(tmp_path: Path, *, structured: bool) -> None:
    """No MindRoom reply shows citations, so a document never asks for them, with or without structured output."""
    wire = Wire(sse_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    provider = route_provider("anthropic", tmp_path, wire)
    document = File(content=b"Paris is in France.", mime_type="text/plain")
    messages = [Message(role="user", content="Where is Paris?", files=[document])]
    request = ModelRequest(messages=messages, output_schema=_Answer if structured else None)

    await claude_turn(provider, request)

    body = wire.body()
    (block,) = [block for block in body["messages"][-1]["content"] if block["type"] == "document"]
    assert ("citations" in block, "output_config" in body) == (False, structured)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "model_id"),
    [
        ("bedrock_claude", "anthropic.claude-haiku-4-5"),
        ("vertexai_claude", "claude-opus-4-6"),
        ("anthropic", "claude-3-5-haiku-latest"),
    ],
)
async def test_a_route_without_native_structured_output_forces_the_answer_tool(
    tmp_path: Path,
    route: str,
    model_id: str,
) -> None:
    """Where the model accepts forced tool use, the output schema goes as the one tool the request forces."""
    call = {"type": "tool_use", "id": "toolu_1", "name": "structured_output", "input": {"city": "Paris"}}
    wire = Wire(sse_reply([call], stop_reason="tool_use"))
    provider = route_provider(route, tmp_path, wire, id=model_id)

    completed = await claude_turn(provider, ModelRequest(messages=_user(), output_schema=_Answer))

    body = wire.body()
    assert provider.capabilities.structured_output == "forced_tool"
    assert body["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert [(tool["name"], tool["input_schema"]) for tool in body["tools"]] == [
        ("structured_output", _Answer.model_json_schema()),
    ]
    assert "output_config" not in body
    assert [json.loads(call["function"]["arguments"]) for call in completed.message.tool_calls] == [{"city": "Paris"}]


@pytest.mark.parametrize(
    ("model_id", "extra_kwargs"),
    [
        ("anthropic.claude-sonnet-5-5", {}),
        ("anthropic.claude-opus-5-5", {}),
        ("anthropic.claude-haiku-4-5", {"thinking": {"type": "enabled", "budget_tokens": 2048}}),
    ],
    ids=["sonnet-5-5", "opus-5-5", "manual-thinking"],
)
def test_a_model_that_rejects_forced_tool_use_answers_in_json_mode(
    tmp_path: Path,
    model_id: str,
    extra_kwargs: dict[str, Any],
) -> None:
    """Some models reject forced tool use on every request, and manual extended thinking rejects it on every model."""
    provider = route_provider("bedrock_claude", tmp_path, Wire(sse_reply([])), id=model_id, extra_kwargs=extra_kwargs)

    assert provider.capabilities.structured_output == "json_mode"


@pytest.mark.asyncio
async def test_vertex_route_strips_strict_and_answers_in_json_mode(tmp_path: Path) -> None:
    """Vertex rejects provider-level strict fields and has no structured output, so the schema stays out."""
    wire = Wire(sse_reply([{"type": "text", "text": "{}"}]))
    provider = route_provider("vertexai_claude", tmp_path, wire)
    tools = [
        ToolSpec(
            name="pick",
            description="Pick.",
            parameters={"type": "object", "properties": {}},
            strict=True,
        ),
    ]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools, output_schema=_Answer))
    request = wire.requests[0]
    assert request.url.path.endswith(":streamRawPredict")
    assert request.headers["anthropic-beta"] == _THINKING_BINDING_BETA
    body = wire.body()
    assert "strict" not in body["tools"][0]
    assert "output_config" not in body


@pytest.mark.asyncio
async def test_a_beta_authored_in_request_params_stays_beside_the_thinking_binding_beta(tmp_path: Path) -> None:
    """A beta an operator writes into ``request_params`` is still sent when MindRoom adds the binding beta."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider(
        "anthropic",
        tmp_path,
        wire,
        extra_kwargs={"request_params": {"betas": ["authored-beta-2026-01-01"]}},
    )
    await claude_turn(provider, ModelRequest(messages=_user()))
    assert wire.requests[0].headers["anthropic-beta"] == f"authored-beta-2026-01-01,{_THINKING_BINDING_BETA}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "extra_kwargs"),
    [
        pytest.param(
            "anthropic",
            {"request_params": {"extra_headers": {"Anthropic-Beta": "mcp-a, mcp-b", "x-trace": "1"}}},
            id="request-headers",
        ),
        pytest.param(
            "anthropic",
            {"default_headers": {"anthropic-beta": "mcp-a,mcp-b", "x-trace": "1"}},
            id="defaults",
        ),
        pytest.param(
            "anthropic",
            {"client_params": {"default_headers": {"ANTHROPIC-BETA": "mcp-a,mcp-b", "x-trace": "1"}}},
            id="client-params-defaults",
        ),
        pytest.param(
            "vertexai_claude",
            {"request_params": {"extra_headers": {"anthropic-beta": "mcp-a,mcp-b", "x-trace": "1"}}},
            id="vertex-request-headers",
        ),
        pytest.param(
            "bedrock_claude",
            {"default_headers": {"anthropic-beta": "mcp-a,mcp-b", "x-trace": "1"}},
            id="bedrock-defaults",
        ),
    ],
)
async def test_a_beta_authored_in_a_header_map_joins_the_request_s_betas_in_one_header(
    tmp_path: Path,
    route: str,
    extra_kwargs: dict[str, Any],
) -> None:
    """A request's ``extra_headers`` would replace the betas the SDK sends, and those would replace a default one.

    So an authored ``anthropic-beta`` joins the request's betas, first, and the request sends exactly one header; the
    other authored headers go out unchanged.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider(route, tmp_path, wire, extra_kwargs={"betas": ["mcp-b"], **extra_kwargs})
    await claude_turn(provider, ModelRequest(messages=_user()))
    headers = wire.requests[0].headers
    added = [] if route == "bedrock_claude" else [_THINKING_BINDING_BETA]
    assert [value for name, value in headers.multi_items() if name.lower() == "anthropic-beta"] == [
        ",".join(["mcp-b", "mcp-a", *added]),
    ]
    assert headers["x-trace"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "bound"),
    [("anthropic", True), ("vertexai_claude", True), ("bedrock_claude", False)],
)
async def test_replayed_thinking_whose_context_changed_is_dropped_where_the_route_takes_the_binding(
    tmp_path: Path,
    route: str,
    *,
    bound: bool,
) -> None:
    """A request whose earlier context changed drops the stale thinking instead of failing, on routes that accept it.

    Sonnet 5.5 checks replayed thinking against the system prompt, tools, and messages before it, which the date in
    MindRoom's system prompt, compaction, and replay limits change; Bedrock has not shown that it accepts the beta, so
    it sends no binding.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    await claude_turn(route_provider(route, tmp_path, wire), ModelRequest(messages=_user()))
    request, body = wire.requests[0], wire.body()
    if bound:
        assert request.headers["anthropic-beta"] == _THINKING_BINDING_BETA
        assert body["thinking"] == {"type": "adaptive", "block_binding": _DROP_STALE}
    else:
        assert "anthropic-beta" not in request.headers
        assert "thinking" not in body


_EARLIER_THINKING = {"type": "thinking", "thinking": "", "signature": "sig-earlier"}
_LOOP_THINKING = {"type": "thinking", "thinking": "", "signature": "sig-loop"}
_LOOP_REDACTED = {"type": "redacted_thinking", "data": "opaque"}
_LOOP_CALL = {"type": "tool_use", "id": "toolu_1", "name": "look", "input": {}}


def _two_turns_with_a_tool_loop() -> list[Message]:
    """An earlier answered turn, then the current turn's first tool call and its result, each reply with thinking."""
    call = {"id": "toolu_1", "type": "function", "function": {"name": "look", "arguments": "{}"}}
    return [
        *_user("First?"),
        Message(
            role="assistant",
            content="First answer.",
            provider_data={"claude_blocks": [dict(_EARLIER_THINKING), {"type": "text", "text": "First answer."}]},
        ),
        Message(role="user", content="Second?"),
        Message(
            role="assistant",
            tool_calls=[call],
            provider_data={"claude_blocks": [dict(_LOOP_THINKING), dict(_LOOP_REDACTED), dict(_LOOP_CALL)]},
        ),
        Message(role="tool", content="Seen.", tool_call_id="toolu_1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "model_id", "extra_kwargs", "bound", "earlier_kept"),
    [
        pytest.param("anthropic", "claude-sonnet-5-5", {}, True, True, id="anthropic-bound"),
        pytest.param("vertexai_claude", "claude-sonnet-5-5", {}, True, True, id="vertex-bound"),
        pytest.param(
            "anthropic",
            "claude-opus-5-5",
            {
                "thinking": {"type": "between_tools"},
                "request_params": {"extra_body": {"thinking": {"type": "adaptive"}}},
            },
            True,
            True,
            id="raw-override-bound",
        ),
        pytest.param(
            "anthropic",
            "claude-sonnet-5-5",
            {"thinking": {"type": "between_tools"}},
            False,
            False,
            id="between-tools",
        ),
        pytest.param(
            "vertexai_claude",
            "claude-sonnet-5-5",
            {"thinking": {"type": "between_tools"}},
            False,
            False,
            id="vertex-between-tools",
        ),
        pytest.param(
            "anthropic",
            "claude-haiku-5-5",
            {"thinking": {"type": "disabled"}},
            False,
            False,
            id="haiku-disabled",
        ),
        pytest.param("bedrock_claude", "anthropic.claude-sonnet-5-5", {}, False, False, id="bedrock"),
        pytest.param(
            "bedrock_claude",
            "anthropic.claude-sonnet-5-5",
            {"thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}},
            True,
            True,
            id="bedrock-authored-binding",
        ),
        pytest.param(
            "bedrock_claude",
            "anthropic.claude-sonnet-5-5",
            {"thinking": {"type": "adaptive", "block_binding": None}},
            False,
            False,
            id="bedrock-null-binding",
        ),
        pytest.param("bedrock_claude", "anthropic.claude-sonnet-5", {}, False, True, id="bedrock-unchecked-model"),
    ],
)
async def test_a_request_without_the_binding_replays_thinking_only_inside_the_current_tool_loop(
    tmp_path: Path,
    route: str,
    model_id: str,
    extra_kwargs: dict[str, Any],
    *,
    bound: bool,
    earlier_kept: bool,
) -> None:
    """Where no binding is sent, a stale earlier block would fail the request, so earlier turns lose thinking.

    The API requires the current tool loop's thinking exactly as received and allows leaving out earlier turns'.
    An authored binding is the operator's choice and gets the beta it needs on every route, and a model without
    the check keeps every block.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "Done."}]))
    provider = route_provider(route, tmp_path, wire, id=model_id, extra_kwargs=extra_kwargs)
    await claude_turn(provider, ModelRequest(messages=_two_turns_with_a_tool_loop()))
    assistant_turns = [
        [{key: value for key, value in block.items() if key != "cache_control"} for block in message["content"]]
        for message in wire.body()["messages"]
        if message["role"] == "assistant"
    ]
    earlier = [{"type": "text", "text": "First answer."}]
    assert assistant_turns == [
        [_EARLIER_THINKING, *earlier] if earlier_kept else earlier,
        [_LOOP_THINKING, _LOOP_REDACTED, _LOOP_CALL],
    ]
    assert (wire.requests[0].headers.get("anthropic-beta") == _THINKING_BINDING_BETA) is bound
    assert ("block_binding" in (wire.body().get("thinking") or {})) is bound


@pytest.mark.asyncio
async def test_an_earlier_turn_of_only_thinking_is_left_out_whole(tmp_path: Path) -> None:
    """The API rejects an empty assistant turn, so one that had only thinking goes, and the user turns around it merge."""
    wire = Wire(sse_reply([{"type": "text", "text": "Done."}]))
    thought = Message(role="assistant", provider_data={"claude_blocks": [dict(_EARLIER_THINKING)]})
    messages = [*_user("First?"), thought, Message(role="user", content="Second?")]
    await claude_turn(route_provider("bedrock_claude", tmp_path, wire), ModelRequest(messages=messages))
    (turn,) = wire.body()["messages"]
    assert turn["role"] == "user"
    assert [block["text"] for block in turn["content"]] == ["First?", "Second?"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "extra_kwargs", "overrides", "sent"),
    [
        pytest.param(
            "claude-fable-5-1",
            {"thinking": {"type": "adaptive", "display": "summarized"}},
            RequestOverrides(),
            {"type": "adaptive", "display": "summarized", "block_binding": _DROP_STALE},
            id="authored-adaptive",
        ),
        pytest.param(
            "claude-opus-5-5",
            {"thinking": {"type": "adaptive"}, "request_params": {"extra_body": {"thinking": {"type": "adaptive"}}}},
            RequestOverrides(),
            {"type": "adaptive", "block_binding": _DROP_STALE},
            id="raw-override",
        ),
        pytest.param(
            "claude-haiku-5-5",
            {"thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}},
            RequestOverrides(),
            {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}},
            id="authored-binding",
        ),
        pytest.param(
            "claude-haiku-5-5",
            {"thinking": {"type": "adaptive", "block_binding": {}}},
            RequestOverrides(),
            {"type": "adaptive", "block_binding": {}},
            id="authored-default-binding",
        ),
        pytest.param(
            "claude-sonnet-5-5",
            {"thinking": {"type": "adaptive", "block_binding": None}},
            RequestOverrides(),
            {"type": "adaptive", "block_binding": _DROP_STALE},
            id="authored-null-binding",
        ),
        pytest.param(
            "claude-opus-5",
            {"thinking": {"type": "adaptive", "block_binding": None}},
            RequestOverrides(),
            {"type": "adaptive"},
            id="unchecked-null-binding",
        ),
        pytest.param(
            "anthropic/claude-sonnet-5-5",
            {},
            RequestOverrides(),
            {"type": "adaptive", "block_binding": _DROP_STALE},
            id="gateway-id",
        ),
        pytest.param(
            "claude-sonnet-5-5",
            {"thinking": {"type": "between_tools"}},
            RequestOverrides(),
            {"type": "between_tools"},
            id="between-tools",
        ),
        pytest.param(
            "claude-sonnet-5-5",
            {},
            RequestOverrides(omit_thinking_setting=True),
            None,
            id="thinking-setting-omitted",
        ),
        pytest.param("claude-opus-5", {}, RequestOverrides(), None, id="unchecked-model"),
        pytest.param("claude-mythos-5-1", {}, RequestOverrides(), None, id="unchecked-mythos"),
    ],
)
async def test_the_thinking_binding_rides_on_adaptive_thinking_of_checked_models(
    tmp_path: Path,
    model_id: str,
    extra_kwargs: dict[str, Any],
    overrides: RequestOverrides,
    sent: dict[str, Any] | None,
) -> None:
    """The API rejects a binding beside other thinking types, an authored one is kept, and older models get none."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    provider = route_provider("anthropic", tmp_path, wire, id=model_id, extra_kwargs=extra_kwargs)
    await claude_turn(provider, ModelRequest(messages=_user(), overrides=overrides))
    assert wire.body().get("thinking") == sent
    bound = sent is not None and "block_binding" in sent
    assert (wire.requests[0].headers.get("anthropic-beta") == _THINKING_BINDING_BETA) is bound


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["message_start", "message_delta"])
async def test_a_reply_that_dropped_replayed_thinking_says_so_in_the_log(tmp_path: Path, event: str) -> None:
    """Only blocks the API dropped are logged; a block it let through despite the changed context is not.

    The API reports them on ``message_start``, and on the final ``message_delta`` after a mid-stream fallback.
    """
    events = claude_stream_events(claude_message([{"type": "text", "text": "Hi."}]))
    reported = events[0]["message"] if event == "message_start" else events[-2]
    reported["input_transformations"] = [
        {"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"},
        {"type": "thinking_dropped", "path": "messages.3.content.0", "reason": "model_binding_mismatch"},
        {"type": "thinking_mismatch_allowed", "path": "messages.5.content.0", "reason": "prefix_binding_mismatch"},
    ]
    wire = Wire(sse_response(events, named=True))
    with capture_logs() as logs:
        await claude_turn(route_provider("vertexai_claude", tmp_path, wire), ModelRequest(messages=_user()))
    (entry,) = [log for log in logs if log["event"] == "claude_replayed_thinking_dropped"]
    assert (entry["model_id"], entry["blocks"], entry["reasons"]) == (
        "claude-sonnet-5-5",
        2,
        ["model_binding_mismatch", "prefix_binding_mismatch"],
    )


@pytest.mark.parametrize(
    ("model_id", "checked"),
    [
        ("claude-fable-5-1", True),
        ("anthropic.claude-opus-5-5", True),
        ("claude-sonnet-5-5@20261001", True),
        ("anthropic/claude-haiku-5-5", True),
        ("claude-fable-5", False),
        ("claude-mythos-5-1", False),
        ("claude-sonnet-4-5", False),
    ],
)
def test_the_thinking_prefix_check_reads_the_model_behind_route_decorated_ids(model_id: str, *, checked: bool) -> None:
    """Fable 5.1 and the 5.5 models check replayed thinking under every route's spelling; older models and Mythos 5.1 do not."""
    assert checks_thinking_prefix(model_id) is checked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "strict_sent", "url_suffix", "beta_header"),
    [
        ("anthropic", True, "/v1/messages?beta=true", f"structured-outputs-2025-11-13,{_THINKING_BINDING_BETA}"),
        ("vertexai_claude", False, ":streamRawPredict", _THINKING_BINDING_BETA),
        ("bedrock_claude", True, "/v1/messages?beta=true", None),
    ],
)
async def test_tool_definitions_send_each_schema_as_authored(
    tmp_path: Path,
    route: str,
    strict_sent: bool,
    url_suffix: str,
    beta_header: str | None,
) -> None:
    """Schemas go as authored, an unset description not at all, and a strict tool's objects closed where strict counts.

    A strict tool, on a route that takes it, closes its objects as strict tool use requires.

    The direct API adds the structured-outputs beta header for a strict tool; Bedrock sends the strict tool
    through the beta endpoint without one.
    """
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider(route, tmp_path, wire, extra_kwargs={"cache_system_prompt": False})
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    tools = [
        ToolSpec(
            name="unset",
            description=None,
            parameters=schema,
            strict=None,
        ),
        ToolSpec(
            name="gated",
            description="Gated.",
            parameters=schema,
            strict=True,
        ),
        ToolSpec(
            name="plain",
            description="Plain.",
            parameters=schema,
            strict=False,
        ),
    ]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools))
    closed = {**schema, "additionalProperties": False}
    assert wire.body()["tools"] == [
        {"name": "gated", "description": "Gated.", "input_schema": closed if strict_sent else schema}
        | ({"strict": True} if strict_sent else {}),
        {"name": "plain", "description": "Plain.", "input_schema": schema},
        {"name": "unset", "input_schema": schema},
    ]
    request = wire.requests[0]
    assert str(request.url).endswith(url_suffix)
    assert request.headers.get("anthropic-beta") == beta_header


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude", "bedrock_claude"])
async def test_a_union_input_schema_reaches_claude_as_an_object_without_its_root_composition(
    tmp_path: Path,
    route: str,
) -> None:
    """Claude rejects ``anyOf``, ``allOf``, and ``oneOf`` at a schema's root, which an MCP tool with a union input has."""
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider(route, tmp_path, wire, extra_kwargs={"cache_system_prompt": False})
    branch = {"type": "object", "properties": {"city": {"$ref": "#/$defs/city"}}, "required": ["city"]}
    union = {"anyOf": [branch, {"type": "object", "properties": {"zip": {"type": "string"}}}]}
    tools = [
        ToolSpec(
            name="lookup",
            description="Look up.",
            parameters={**union, "$defs": {"city": {"type": "string"}}},
            strict=None,
        ),
        ToolSpec(
            name="typed",
            description="Typed.",
            parameters={"type": "object", "oneOf": [branch], "allOf": []},
            strict=None,
        ),
    ]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools))
    assert [tool["input_schema"] for tool in wire.body()["tools"]] == [
        {"type": "object", "$defs": {"city": {"type": "string"}}},
        {"type": "object"},
    ]


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
    wire = Wire(sse_reply([{"type": "text", "text": "{}"}]))
    authored = {"request_params": {"tool_choice": {"type": "auto"}, "extra_body": {"tool_choice": {"type": "any"}}}}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=authored)
    tools = [
        ToolSpec(
            name="get_weather",
            description="Forecast.",
            parameters={"type": "object", "properties": {}},
            strict=None,
        ),
    ]
    await claude_turn(provider, ModelRequest(messages=_user(), tools=tools, tool_choice=tool_choice))
    assert wire.body()["tool_choice"] == expected


@pytest.mark.asyncio
async def test_tool_choice_is_left_out_of_a_request_without_tools(tmp_path: Path) -> None:
    """The API rejects a tool choice without tools, so a decision request that offers none sends no choice."""
    wire = Wire(sse_reply([{"type": "text", "text": "{}"}]))
    await claude_turn(
        route_provider("anthropic", tmp_path, wire),
        ModelRequest(messages=_user(), tool_choice="none"),
    )
    assert "tool_choice" not in wire.body()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_source", ["extra_body", "mcp_servers", "function_extra_body"])
async def test_claude_native_tools_cannot_execute_during_decision(tmp_path: Path, native_source: str) -> None:
    """A decision turns tool selection off for server tools and MCP servers authored in raw request params too.

    The SDK merges ``extra_body`` over the named fields, so what it carries counts as offered. The reply keeps the
    authored choice and offers the same tools.
    """
    request_params: dict[str, Any] = {"tool_choice": {"type": "auto"}}
    extra_kwargs: dict[str, Any] = {"request_params": request_params}
    tools: list[ToolSpec] = []
    if native_source == "extra_body":
        request_params["extra_body"] = {
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "auto"},
        }
    elif native_source == "mcp_servers":
        request_params["mcp_servers"] = [{"type": "url", "url": "https://example.com/mcp", "name": "remote"}]
        extra_kwargs["betas"] = ["mcp-client-2025-04-04"]
    else:
        request_params["extra_body"] = {"tool_choice": {"type": "auto"}}
        tools = [
            ToolSpec(
                name="lookup",
                description="Look it up.",
                parameters={"type": "object", "properties": {}},
                strict=None,
            ),
        ]
    wire = Wire(sse_reply([{"type": "text", "text": "{}"}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=extra_kwargs)
    for tool_choice in ("none", None):
        await claude_turn(provider, ModelRequest(messages=_user(), tools=tools, tool_choice=tool_choice))

    decision, reply = wire.body(0), wire.body(1)
    assert decision["tool_choice"] == {"type": "none"}
    assert reply["tool_choice"] == {"type": "auto"}
    assert (decision.get("tools"), decision.get("mcp_servers")) == (reply.get("tools"), reply.get("mcp_servers"))


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
        sse_reply([checkpoint, {"type": "text", "text": "Done."}], usage=usage),
        sse_reply([{"type": "text", "text": "Next."}]),
    )
    provider = route_provider("anthropic", tmp_path, wire)
    replay = native_settings(provider, history_generation="gen-1")
    assert replay is not None
    assert replay.threshold == 60000
    assert native_settings(provider, threshold=1000) is None, "Claude needs at least 50,000 input tokens"
    assert native_settings(provider, history_generation="gen-2") != replay

    first = await claude_turn(
        provider,
        ModelRequest(
            messages=_user("Long history."),
            native_compaction=replay,
        ),
    )
    request = wire.requests[0]
    assert request.headers["anthropic-beta"] == f"compact-2026-01-12,{_THINKING_BINDING_BETA}"
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
    assert metrics.context == TokenCounts(input_tokens=40, cache_read_tokens=5, cache_write_tokens=3)

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
    assert (first.message.provider_data or {})["claude_blocks"][0]["type"] == "compaction"
    assert messages[0].content == "Be brief."


@pytest.mark.asyncio
async def test_authored_compaction_edits_keep_their_policy_and_cannot_pause(tmp_path: Path) -> None:
    """A model that authored its own compaction edit replays checkpoints without MindRoom's trigger."""
    authored = {"edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 80000}}]}
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": authored})
    assert provider.capabilities.native_compaction is False
    assert native_settings(provider) is None
    replay = native_settings(provider, allow_authored=True)
    assert replay is not None
    assert replay.threshold is None
    await claude_turn(provider, ModelRequest(messages=_user(), native_compaction=replay))
    assert wire.body()["context_management"] == authored
    assert wire.requests[0].headers["anthropic-beta"] == f"compact-2026-01-12,{_THINKING_BINDING_BETA}"

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
        Wire(sse_reply([])),
        extra_kwargs={"context_management": authored},
    )
    assert provider.capabilities.native_compaction is False
    assert native_settings(provider, allow_authored=True) is None
    assert native_settings(provider, threshold=None, allow_authored=True) is None


@pytest.mark.asyncio
async def test_an_authored_context_edit_records_what_the_response_applied(tmp_path: Path) -> None:
    """A model authoring context editing keeps the edits each response applied with its message."""
    applied = {
        "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3, "cleared_input_tokens": 12000}],
    }
    wire = Wire(sse_reply([{"type": "text", "text": "Ok."}], context_management=applied))
    authored = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": authored})
    completed = await claude_turn(provider, ModelRequest(messages=_user()))
    assert str(wire.requests[0].url).endswith("?beta=true")
    assert wire.body()["context_management"] == authored
    assert (completed.message.provider_data or {})["context_management"] == applied


@pytest.mark.asyncio
async def test_replay_drops_unsigned_thinking_from_a_cut_off_response(tmp_path: Path) -> None:
    """A stream that stopped mid-block stores an unsigned thinking block, which the API rejects on replay."""
    signed = {"type": "thinking", "thinking": "", "signature": "complete"}
    unsigned = {"type": "thinking", "thinking": "", "signature": ""}
    tool_use = {"type": "tool_use", "id": "toolu_save", "name": "save_file", "input": {"file_name": "notes.md"}}
    unfinished = {"type": "tool_use", "id": "toolu_shell", "name": "run_shell_command", "input": {}}
    stored = [signed, tool_use, unsigned, unfinished]
    wire = Wire(
        sse_reply([dict(block) for block in stored], stop_reason="max_tokens"),
        sse_reply([{"type": "text", "text": "Saved."}]),
    )
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    question = Message(role="user", content="Save the notes.")
    cut_off = (await claude_turn(provider, ModelRequest(messages=[question]))).message
    assert (cut_off.provider_data or {})["claude_blocks"] == stored
    # Only the save call ran; the cut-off shell call left no result.
    cut_off.tool_calls = [call for call in cut_off.tool_calls or [] if call["id"] == "toolu_save"]
    messages = [question, cut_off, Message(role="tool", tool_call_id="toolu_save", content="saved")]
    await claude_turn(provider, ModelRequest(messages=messages))
    assert wire.body()["messages"][1]["content"] == [signed, tool_use]
    assert (messages[1].provider_data or {})["claude_blocks"] == stored


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
    assert (static.settings.aws_access_key, static.settings.session) == ("AKIA-static", None)
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
        assert vertex.identity.endpoint == "https://vertex-gateway.example/v1|env-project|europe-west1"
        assert (
            provider_for(
                {"provider": "vertexai_claude", "id": "claude-sonnet-5-5"},
                tmp_path,
                wire.client,
            ).settings.region
            is None
        )


def test_bedrock_reads_the_region_variables_in_the_order_aws_tools_read_them(tmp_path: Path) -> None:
    """Both region variables are tried in the process environment before either in ``.env``, as AWS tools try them."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AWS_REGION=us-west-2\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={"AWS_DEFAULT_REGION": "eu-central-1"},
    )
    keys = {"aws_access_key": "AKIA-static", "aws_secret_key": "static-secret"}
    with isolated_provider_environment():
        bedrock = build_provider(
            ModelConfig(provider="bedrock_claude", id="anthropic.claude-sonnet-5-5", extra_kwargs=keys),
            runtime_paths,
        )
    assert bedrock.settings.aws_region == "eu-central-1"


def test_anthropic_base_url_sets_the_direct_endpoint_only_where_client_params_set_none(tmp_path: Path) -> None:
    """``ANTHROPIC_BASE_URL`` from the runtime env is the direct route's endpoint unless the model configures one."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={"ANTHROPIC_BASE_URL": "https://env-gateway.example/"},
    )
    own = {"client_params": {"base_url": "https://own-gateway.example"}}
    with isolated_provider_environment():
        from_env = build_provider(ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="k"), runtime_paths)
        configured = build_provider(
            ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="k", extra_kwargs=own),
            runtime_paths,
        )
    assert from_env.identity.endpoint == "https://env-gateway.example"
    assert configured.identity.endpoint == "https://own-gateway.example"
    assert configured.settings.client_params == own["client_params"]


def test_a_blank_exported_anthropic_base_url_keeps_the_direct_route_on_anthropics_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK reads ``ANTHROPIC_BASE_URL`` itself when given none, and would take a blank export as its endpoint."""
    config_path = tmp_path / "config.yaml"
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={"ANTHROPIC_BASE_URL": ""},
    )
    hide_provider_environment(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    own = {"client_params": {"base_url": "https://own-gateway.example"}}
    default = build_provider(ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="k"), runtime_paths)
    configured = build_provider(
        ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="k", extra_kwargs=own),
        runtime_paths,
    )

    assert str(default._client().base_url).rstrip("/") == "https://api.anthropic.com"
    assert str(configured._client().base_url).rstrip("/") == "https://own-gateway.example"


@pytest.mark.parametrize(
    ("name", "setting", "value", "fix"),
    [
        ("ANTHROPIC_VERTEX_BASE_URL", "base_url", "https://vertex.example/v1", "unset it for the region's endpoint"),
        ("CLOUD_ML_REGION", "region", "europe-west1", "set a region"),
    ],
)
def test_a_blank_exported_vertex_endpoint_or_region_fails_the_build_and_names_the_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    setting: str,
    value: str,
    fix: str,
) -> None:
    """The Vertex SDK reads these itself when given none, and would build every request URL from a blank export."""
    config_path = tmp_path / "config.yaml"
    env = {"ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "us-east5", name: ""}
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)
    hide_provider_environment(monkeypatch)
    model = ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5")

    with pytest.raises(ValueError, match=f"{name} is exported blank, .*; {fix}"):
        build_provider(model, runtime_paths)
    # A value the model or the `.env` sets reaches the SDK, which then reads nothing from the environment.
    for extra_kwargs in ({setting: value}, {"client_params": {setting: value}}):
        build_provider(model.model_copy(update={"extra_kwargs": extra_kwargs}), runtime_paths)
    (tmp_path / ".env").write_text(f"{name}={value}\n", encoding="utf-8")
    env_file_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)
    assert getattr(build_provider(model, env_file_paths).settings, setting) == value


def test_a_blank_exported_vertex_project_still_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Vertex SDK reads a blank project as unset and takes the credentials' project, so the build goes on."""
    config_path = tmp_path / "config.yaml"
    env = {"ANTHROPIC_VERTEX_PROJECT_ID": "", "CLOUD_ML_REGION": "us-east5"}
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)
    hide_provider_environment(monkeypatch)

    vertex = build_provider(ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5"), runtime_paths)

    assert (vertex.settings.project_id, vertex.settings.region) == (None, "us-east5")


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
    assert str(request.url) == "https://gateway.example/anthropic/v1/messages?beta=true"
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
    # The endpoint identity names the endpoint the client reaches.
    assert (direct.identity.endpoint, vertex.identity.endpoint) == (
        "https://gateway.example/anthropic",
        "https://vertex.example/v1|params-project|eu",
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
async def test_requests_keep_the_sdk_timeout_unless_the_model_authors_one(tmp_path: Path) -> None:
    """Every request streams, so the SDK's own limits apply until the model authors a timeout."""
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    request = ModelRequest(messages=_user())
    await claude_turn(route_provider("anthropic", tmp_path, wire), request)
    await claude_turn(route_provider("anthropic", tmp_path, wire, extra_kwargs={"timeout": 120.0}), request)
    assert [sent.extensions["timeout"] for sent in wire.requests] == [
        anthropic.DEFAULT_TIMEOUT.as_dict(),
        httpx.Timeout(120.0).as_dict(),
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
async def test_the_stable_prompt_stays_cacheable_when_the_turn_context_changes(
    tmp_path: Path,
    route: str,
    cache: str,
) -> None:
    """The stable system message is its own marked block, so a new date in the turn part reuses the cached prefix."""
    settings = {"extended_cache_time": cache == "extended", "cache_system_prompt": cache != "disabled"}
    bodies = []
    for day in ("Monday", "Tuesday"):
        wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
        (tmp_path / day).mkdir()
        provider = route_provider(route, tmp_path / day, wire, extra_kwargs=settings)
        messages = [
            Message(role="system", content="Shared instructions."),
            Message(role="system", content=f"Current date: {day}"),
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
    assert monday["system"][0] == {"type": "text", "text": "Shared instructions.", "cache_control": cache_control}
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
    wire = Wire(sse_reply([{"type": "text", "text": "Hi."}]))
    settings = {"cache_system_prompt": True, "extended_cache_time": True, "request_params": {"system": authored_system}}
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs=settings)
    await claude_turn(provider, ModelRequest(messages=[Message(role="user", content="Hello")]))
    assert wire.body()["system"] == authored_system
