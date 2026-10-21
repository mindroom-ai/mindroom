"""Behavior of the owned Claude adapter beyond the goldens: settings, events, stop reasons, errors, overrides, replay."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import BaseModel

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import ContextWindowExceeded, InvalidRequest, ProviderUnavailable, RateLimited
from mindroom.llm.media import Audio
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    ModelRequest,
    ReasoningDelta,
    RequestOverrides,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
)
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider
from mindroom.providers.anthropic.settings import parse_claude_settings
from tests.providers.anthropic_family_support import encoded_response, isolated_provider_environment, provider_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ROUTE_CONFIGS: dict[str, dict[str, Any]] = {
    "anthropic": {"provider": "anthropic", "id": "claude-sonnet-5-5", "api_key": "test-key"},
    "vertexai_claude": {
        "provider": "vertexai_claude",
        "id": "claude-sonnet-5-5",
        "extra_kwargs": {"project_id": "test-project", "region": "us-east5"},
    },
    "bedrock_claude": {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5-5",
        "extra_kwargs": {"aws_region": "us-east-1", "aws_access_key": "test-access", "aws_secret_key": "test-secret"},
    },
}
_USAGE = {"input_tokens": 40, "output_tokens": 9, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}


class _Wire:
    """Records the requests one adapter sends and answers each from a queue."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._respond))

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return response if isinstance(response, httpx.Response) else response(request)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _message(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or _USAGE,
    }


def _json_reply(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
) -> httpx.Response:
    return httpx.Response(200, json=_message(blocks, stop_reason=stop_reason, usage=usage))


def _sse_reply(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
) -> httpx.Response:
    """Stream the blocks the way the API does: a start event, deltas per block, the stop reason, the stop."""
    final_usage = usage or _USAGE
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": _message([], stop_reason="end_turn", usage={**final_usage, "output_tokens": 1})
            | {"stop_reason": None},
        },
    ]
    for index, block in enumerate(blocks):
        kind = block["type"]
        deltas: list[dict[str, Any]]
        if kind == "text":
            start: dict[str, Any] = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif kind == "tool_use":
            start = {**block, "input": {}}
            deltas = [
                {"type": "input_json_delta", "partial_json": block.pop("partial_json", json.dumps(block["input"]))},
            ]
        else:
            start = block
            deltas = []
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": final_usage["output_tokens"]},
        },
    )
    events.append({"type": "message_stop"})
    return encoded_response(
        {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "sse": [{"event": event["type"], "data": event} for event in events],
        },
    )


def _provider(route: str, tmp_path: Path, wire: _Wire, **config: Any) -> ClaudeProvider:  # noqa: ANN401
    model_config = {**_ROUTE_CONFIGS[route], **config}
    if "extra_kwargs" in config:
        model_config["extra_kwargs"] = {**_ROUTE_CONFIGS[route].get("extra_kwargs", {}), **config["extra_kwargs"]}
    return provider_for(model_config, tmp_path, wire.client)


def _user(text: str = "Hello") -> list[Message]:
    return [Message(role="system", content="Be brief."), Message(role="user", content=text)]


async def _events(provider: ClaudeProvider, request: ModelRequest) -> list[Any]:
    with isolated_provider_environment():
        return [event async for event in provider.stream(request)]


async def _turn(provider: ClaudeProvider, request: ModelRequest) -> TurnCompleted:
    events = await _events(provider, request)
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed


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
    provider = _provider(route, tmp_path, _Wire(_json_reply([])))
    assert (provider.identity.provider, provider.identity.provider_name) == (provider_name, provider_name)
    capabilities = provider.capabilities
    assert capabilities.structured_output == structured_output
    assert capabilities.native_tool_search is native_tool_search
    assert capabilities.omitted_media == frozenset({"audio", "video"})
    assert capabilities.claude_family is True
    assert capabilities.prompt_cache_breakpoints is True
    assert capabilities.provider_default_sampling is True
    assert capabilities.native_compaction is (route != "bedrock_claude")


@pytest.mark.asyncio
async def test_stream_reports_start_usage_deltas_tool_calls_and_the_finished_message(tmp_path: Path) -> None:
    """The partial usage comes first, output events follow the blocks, and the turn ends with the parsed message."""
    wire = _Wire(
        _sse_reply(
            [
                {"type": "thinking", "thinking": "Check first.", "signature": "sig"},
                {"type": "text", "text": "Looking it up."},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
            ],
            stop_reason="tool_use",
        ),
    )
    provider = _provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    tools = [ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object", "properties": {}})]
    events = await _events(provider, ModelRequest(messages=_user(), tools=tools))

    assert isinstance(events[0], UsageReported)
    assert events[0].final is False
    assert (events[0].usage.input_tokens, events[0].usage.output_tokens, events[0].usage.cache_read_tokens) == (
        40,
        1,
        5,
    )
    assert events[1] == ReasoningDelta("Check first.")
    assert events[2] == TextDelta("Looking it up.")
    assert events[3] == ToolCallReady(
        {"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
    )
    final_usage, completed = events[4], events[5]
    assert isinstance(final_usage, UsageReported)
    assert final_usage.final is True
    assert isinstance(completed, TurnCompleted)
    assert completed.stop_reason == "tool_use"
    message = completed.message
    assert final_usage.usage is message.metrics
    assert (message.metrics.input_tokens, message.metrics.output_tokens, message.metrics.total_tokens) == (40, 9, 49)
    assert (message.metrics.cache_read_tokens, message.metrics.cache_write_tokens) == (5, 3)
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
async def test_cut_off_response_keeps_its_tool_calls_and_stops_at_the_output_limit(tmp_path: Path) -> None:
    """A call the output limit cut off is kept with the arguments that parsed, so the loop can answer it."""
    truncated = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "save_file",
        "input": {},
        "partial_json": '{"name": "notes.md", "content": "# Not',
    }
    wire = _Wire(_sse_reply([truncated], stop_reason="max_tokens"))
    completed = await _turn(_provider("anthropic", tmp_path, wire), ModelRequest(messages=_user()))
    assert completed.stop_reason == "output_limit"
    assert completed.message.tool_calls == [
        {"id": "toolu_1", "type": "function", "function": {"name": "save_file", "arguments": '{"name": "notes.md"}'}},
    ]
    assert (completed.message.provider_data or {})["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_refusal_completes_with_the_refusal_stop_reason(tmp_path: Path) -> None:
    """A safeguard refusal is a completed turn the loop recognizes by its stop reason, not an adapter error."""
    wire = _Wire(_json_reply([], stop_reason="refusal"))
    provider = _provider("anthropic", tmp_path, wire)
    completed = await _turn(provider, ModelRequest(messages=_user(), response_format={"type": "json_object"}))
    assert completed.stop_reason == "refusal"
    assert completed.message.content is None
    assert completed.message.metrics.input_tokens == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected", "retryable"),
    [
        (429, {"type": "rate_limit_error", "message": "Rate limited"}, RateLimited, True),
        (529, {"type": "overloaded_error", "message": "Overloaded"}, RateLimited, True),
        (503, {"type": "api_error", "message": "Unavailable"}, ProviderUnavailable, True),
        (
            400,
            {"type": "invalid_request_error", "message": "prompt is too long: 300000 tokens"},
            ContextWindowExceeded,
            False,
        ),
        (400, {"type": "invalid_request_error", "message": "messages.0: bad"}, InvalidRequest, False),
        (401, {"type": "authentication_error", "message": "invalid x-api-key"}, InvalidRequest, False),
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
    wire = _Wire(httpx.Response(status, json={"type": "error", "error": body}))
    provider = _provider("anthropic", tmp_path, wire)
    request = ModelRequest(messages=_user(), overrides=RequestOverrides(sdk_max_retries=0))
    with pytest.raises(expected) as raised:
        await _turn(provider, request)
    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert raised.value.provider == "Anthropic"
    assert body["message"] in str(raised.value)
    assert len(wire.requests) == 1, "sdk_max_retries=0 must stop the SDK's own retries"


@pytest.mark.asyncio
async def test_transport_failures_are_retryable_provider_unavailable(tmp_path: Path) -> None:
    """A connection that never reaches the provider is retryable."""

    def fail(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    provider = _provider("anthropic", tmp_path, _Wire(fail))
    with pytest.raises(ProviderUnavailable) as raised:
        await _turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(sdk_max_retries=0)))
    assert raised.value.retryable is True
    assert raised.value.status_code is None


@pytest.mark.asyncio
async def test_summary_overrides_drop_thinking_cache_markers_and_apply_sampling(tmp_path: Path) -> None:
    """One-off summary requests run without thinking or cache writes, with their own limits."""
    wire = _Wire(_sse_reply([{"type": "text", "text": "Summary."}]))
    provider = _provider(
        "anthropic",
        tmp_path,
        wire,
        id="claude-sonnet-4-5",
        extra_kwargs={
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "temperature": 0.7,
            "request_params": {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 2048}, "top_k": 4}},
        },
    )
    overrides = RequestOverrides(temperature=0.1, max_output_tokens=512, prompt_cache=False, disable_thinking=True)
    messages = [*_user("Summarize."), Message(role="assistant", content="Earlier answer.")]
    await _turn(provider, ModelRequest(messages=messages, overrides=overrides))
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
    wire = _Wire(_sse_reply([{"type": "text", "text": "Hi."}]))
    provider = _provider(
        "anthropic",
        tmp_path,
        wire,
        extra_kwargs={"temperature": 0.7, "top_p": 0.9, "request_params": {"extra_body": {"top_k": 3}}},
    )
    await _turn(provider, ModelRequest(messages=_user(), overrides=RequestOverrides(temperature=0.2)))
    body = wire.body()
    assert not {"temperature", "top_p", "top_k"} & body.keys()
    # The conversation ends with a user turn already, and thinking is off, yet Sonnet 5.5 rejects prefill.
    assert body["messages"][-1]["content"][0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_history_ending_with_an_assistant_turn_gets_a_trailing_user_turn(tmp_path: Path) -> None:
    """Models without prefill, and any model with thinking on, need the request to end with a user turn."""
    wire = _Wire(_sse_reply([{"type": "text", "text": "More."}]))
    provider = _provider("anthropic", tmp_path, wire)
    stored = {
        "content_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "A"}],
    }
    messages = [
        *_user(),
        Message(role="assistant", content="A", provider_data=stored),
        Message(role="assistant", content="B"),
    ]
    await _turn(provider, ModelRequest(messages=messages))
    turns = wire.body()["messages"]
    assert turns[-1]["role"] == "user"
    assert [(block["type"], block["text"]) for block in turns[-1]["content"]] == [("text", "continue")]
    # Two assistant turns share one message only without the earlier turn's thinking.
    assert [(block["type"], block["text"]) for block in turns[-2]["content"]] == [("text", "A"), ("text", "B")]


@pytest.mark.asyncio
async def test_audio_inputs_are_not_sent(tmp_path: Path) -> None:
    """Claude takes images and documents; audio stays out of the request for the media fallback to describe."""
    wire = _Wire(_sse_reply([{"type": "text", "text": "Ok."}]))
    provider = _provider("anthropic", tmp_path, wire)
    audio = Audio(content=b"RIFF....WAVE", mime_type="audio/wav")
    await _turn(provider, ModelRequest(messages=[Message(role="user", content="Listen.", audio=[audio])]))
    assert [block["type"] for block in wire.body()["messages"][0]["content"]] == ["text"]


class _Answer(BaseModel):
    city: str


@pytest.mark.asyncio
async def test_direct_route_sends_strict_tools_and_schemas_under_the_structured_outputs_beta(tmp_path: Path) -> None:
    """Strict tools and a pydantic response format use the beta endpoint with its header, without streaming."""
    wire = _Wire(_json_reply([{"type": "text", "text": '{"city": "Paris"}'}]))
    provider = _provider("anthropic", tmp_path, wire)
    tools = [ToolSpec(name="pick", description="Pick.", parameters={"type": "object", "properties": {}}, strict=True)]
    completed = await _turn(provider, ModelRequest(messages=_user(), tools=tools, response_format=_Answer))
    request = wire.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert request.headers["anthropic-beta"] == "structured-outputs-2025-11-13"
    body = wire.body()
    assert "stream" not in body
    assert body["tools"][0]["strict"] is True
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    # Document citations conflict with structured output, so none are requested with it.
    assert completed.message.content == '{"city": "Paris"}'
    assert completed.stop_reason == "end"


@pytest.mark.asyncio
async def test_vertex_route_strips_strict_and_answers_in_json_mode(tmp_path: Path) -> None:
    """Vertex rejects provider-level strict fields and has no structured output, so the schema stays out."""
    wire = _Wire(_json_reply([{"type": "text", "text": "{}"}]))
    provider = _provider("vertexai_claude", tmp_path, wire)
    tools = [ToolSpec(name="pick", description="Pick.", parameters={"type": "object", "properties": {}}, strict=True)]
    await _turn(provider, ModelRequest(messages=_user(), tools=tools, response_format=_Answer))
    request = wire.requests[0]
    assert request.url.path.endswith(":rawPredict")
    assert "anthropic-beta" not in request.headers
    body = wire.body()
    assert "strict" not in body["tools"][0]
    assert "output_config" not in body


@pytest.mark.asyncio
async def test_native_compaction_sends_the_edit_and_replays_the_recorded_checkpoint(tmp_path: Path) -> None:
    """The request asks Claude to compact; the checkpoint it returns replaces the earlier prefix next time."""
    checkpoint = {"type": "compaction", "content": "Summary so far.", "encrypted_content": "opaque"}
    usage = {
        **_USAGE,
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
    wire = _Wire(
        _json_reply([checkpoint, {"type": "text", "text": "Done."}], usage=usage),
        _sse_reply([{"type": "text", "text": "Next."}]),
    )
    provider = _provider("anthropic", tmp_path, wire)
    replay = provider.native_compaction_settings(threshold=60000, history_generation="gen-1")
    assert replay is not None
    assert replay.threshold == 60000
    assert provider.native_compaction_settings(threshold=1000) is None, "Claude needs at least 50,000 input tokens"
    assert provider.native_compaction_settings(threshold=60000, history_generation="gen-2") != replay

    first = await _turn(
        provider,
        ModelRequest(
            messages=_user("Long history."),
            response_format={"type": "json_object"},
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
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (60040, 909, 60949)
    assert metrics.provider_metrics == {
        "context_usage": {"input_tokens": 40, "cache_read_tokens": 5, "cache_write_tokens": 3},
    }

    messages = [*_user("Long history."), first.message, Message(role="user", content="Continue.")]
    second = await _turn(provider, ModelRequest(messages=messages, native_compaction=replay))
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
    wire = _Wire(_sse_reply([{"type": "text", "text": "Ok."}]))
    provider = _provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": authored})
    assert provider.capabilities.native_compaction is False
    assert provider.native_compaction_settings(threshold=60000) is None
    replay = provider.native_compaction_settings(threshold=60000, allow_authored=True)
    assert replay is not None
    assert replay.threshold is None
    await _turn(provider, ModelRequest(messages=_user(), native_compaction=replay))
    assert wire.body()["context_management"] == authored
    assert wire.requests[0].headers["anthropic-beta"] == "compact-2026-01-12"

    paused = {"edits": [{"type": "compact_20260112", "pause_after_compaction": True}]}
    pausing = _provider("anthropic", tmp_path, wire, extra_kwargs={"context_management": paused})
    with pytest.raises(ValueError, match="pause_after_compaction"):
        await _turn(pausing, ModelRequest(messages=_user()))


@pytest.mark.asyncio
async def test_replay_drops_unsigned_thinking_from_a_cut_off_response(tmp_path: Path) -> None:
    """A stream that stopped mid-block leaves an unsigned thinking block, which the API rejects on replay."""
    signed = {"type": "thinking", "thinking": "", "signature": "complete"}
    unsigned = {"type": "thinking", "thinking": "", "signature": ""}
    tool_use = {"type": "tool_use", "id": "toolu_save", "name": "save_file", "input": {"file_name": "notes.md"}}
    unfinished = {"type": "tool_use", "id": "toolu_shell", "name": "run_shell_command", "input": {}}
    stored = [signed, tool_use, unsigned, unfinished]
    wire = _Wire(_sse_reply([{"type": "text", "text": "Saved."}]))
    provider = _provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
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
    await _turn(provider, ModelRequest(messages=messages))
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

    wire = _Wire(_sse_reply([{"type": "text", "text": "Hi."}]))
    config = {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5-5",
        "extra_kwargs": {"session": _Session()},
    }
    provider = provider_for(config, tmp_path, wire.client)
    await _turn(provider, ModelRequest(messages=_user()))
    await _turn(provider, ModelRequest(messages=_user()))
    session = provider.settings.session
    assert isinstance(session, _Session)
    assert session.resolutions == 2
    assert all(request.url.host == "bedrock-mantle.eu-west-1.api.aws" for request in wire.requests)


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
    }
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)
    wire = _Wire(_sse_reply([{"type": "text", "text": "Hi."}]))
    with isolated_provider_environment():
        direct = build_provider(
            ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="config-key"),
            runtime_paths,
        )
        await _turn(replace(direct, http_client=wire.client), ModelRequest(messages=_user()))
        assert wire.requests[-1].headers["x-api-key"] == "config-key"

        bedrock = build_provider(
            ModelConfig(provider="bedrock_claude", id="anthropic.claude-sonnet-5-5"),
            runtime_paths,
        )
        await _turn(replace(bedrock, http_client=wire.client), ModelRequest(messages=_user()))
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
        assert (
            provider_for(
                {"provider": "vertexai_claude", "id": "claude-sonnet-5-5"},
                tmp_path,
                wire.client,
            ).settings.region
            is None
        )


def test_format_tool_results_appends_each_result_for_the_wire_to_merge(tmp_path: Path) -> None:
    """Results stay separate tool messages in history; the formatter merges them into one user turn on the wire."""
    provider = _provider("anthropic", tmp_path, _Wire(_json_reply([])))
    results = [Message(role="tool", tool_call_id="a", content="1"), Message(role="tool", tool_call_id="b", content="2")]
    assert provider.format_tool_results(_user(), results) == results
