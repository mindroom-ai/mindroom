"""OpenAI-family adapter behavior the wire goldens do not reach: legacy history, errors, overrides, routing."""
# ruff: noqa: D103

from __future__ import annotations

import base64
import json
import re
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import openai
import pytest
from openai.types.responses import ResponseOutputItemAddedEvent, ResponseReasoningSummaryTextDeltaEvent
from pydantic import BaseModel
from structlog.testing import capture_logs

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
    DeferredTools,
    ModelRequest,
    NativeCompactionReplay,
    ReasoningDelta,
    RequestOverrides,
    TextDelta,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.native_compaction import native_compaction_route
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.openai import build as openai_build
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.errors import provider_error
from mindroom.providers.openai.response_replay import RESPONSE_OUTPUT_KEY
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import ResponsesStream
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.media import FALLBACK_MARKER
from tests.providers.golden_support import PacedBytes, encoded_response, httpx2_transport, pipeline_provider
from tests.providers.openai_family_support import (
    golden_provider,
    openai_family_adapter,
    provider_runtime,
    unsigned_jwt,
    write_cli_logins,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

_USAGE = {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}
_RESPONSE_USAGE = {"input_tokens": 42, "output_tokens": 12, "total_tokens": 54}
_OPENAI_CHAT = {"provider": "openai", "api": "chat_completions", "id": "gpt-6-astra", "api_key": "test-key"}
_OPENAI_RESPONSES = {"provider": "openai", "api": "responses", "id": "gpt-6-astra", "api_key": "test-key"}
_CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
# A new id a Chat turn stores a call under: when its server sent none, or one outside the id rule Chat requests keep.
_NEW_CHAT_ID = r"call_[0-9a-f]{32}"


def _chunk(delta: dict[str, Any] | None = None, *, finish_reason: str | None = None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    return {
        "data": {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": choices,
            **extra,
        },
    }


def _chat_reply(*chunks: dict[str, Any], status: int = 200) -> dict[str, Any]:
    events = [*chunks, _chunk(usage=_USAGE), {"data": "[DONE]"}]
    return {"status": status, "headers": {"content-type": "text/event-stream"}, "sse": events}


def _json_reply(body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    return {"status": status, "headers": {"content-type": "application/json"}, "json": body}


def _response(status: str = "completed", output: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-astra",
        "status": status,
        "output": output or [],
        "usage": _RESPONSE_USAGE,
        **extra,
    }


def _responses_reply(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": 200,
        "headers": {"content-type": "text/event-stream"},
        "sse": [{"event": event["type"], "data": event} for event in events],
    }


def _text_events(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "sequence_number": 0, "response": _response("in_progress", usage=None)},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
            "logprobs": [],
        },
    ]


def _completed(output: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "response.completed", "sequence_number": 9, "response": _response(output=output)}


class _Built:
    def __init__(
        self,
        tmp_path: Path,
        model_config: dict[str, Any],
        reply: dict[str, Any],
        *,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        built = golden_provider(
            {"input": {"model_config": model_config}, "response_raw": reply},
            tmp_path,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        self.provider: ModelProvider = built.provider
        self.adapter: ModelProvider = built.adapter
        self.requests = built.requests

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _user(text: str = "What should I pack?") -> Message:
    return Message(role="user", content=text)


async def _turn(built: _Built, request: ModelRequest) -> Any:  # noqa: ANN401
    return await complete(built.provider, request)


async def _adapter_turn(built: _Built, request: ModelRequest) -> Any:  # noqa: ANN401
    """Send the request to the bare adapter, past the pipeline's retries."""
    return await complete(built.adapter, request)


# --- Chat parsing ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_on_several_chat_chunks_adds_up(tmp_path: Path) -> None:
    """Each chunk's usage adds to the turn's, so every partial report and the final one hold the running sum.

    Every usage-bearing chunk adds to the assistant message's metrics, so a server that repeats its cumulative
    usage on each chunk is counted once per chunk.
    """
    first = {"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41}
    reply = _chat_reply(_chunk({"content": "ok"}, usage=first), _chunk({}, finish_reason="stop"))
    built = _Built(tmp_path, _OPENAI_CHAT, reply)

    events = [event async for event in built.adapter.stream(ModelRequest(messages=[_user()]))]

    reports = [event for event in events if isinstance(event, UsageReported)]
    assert [
        (report.usage.tokens.input_tokens, report.usage.tokens.output_tokens, report.final) for report in reports
    ] == [
        (40, 1, False),
        (82, 13, False),
        (82, 13, True),
    ]


@pytest.mark.asyncio
async def test_chat_stream_tolerates_sparse_tool_call_indexes(tmp_path: Path) -> None:
    delta = {
        "tool_calls": [
            {
                "index": 1,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": '},
            },
        ],
    }
    more = {"tool_calls": [{"index": 1, "function": {"arguments": '"Paris"}'}}]}
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk(delta), _chunk(more, finish_reason="tool_calls")))

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.tool_calls == [_CALL]
    assert turn.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_chat_stream_places_calls_streamed_without_an_index_by_their_position(tmp_path: Path) -> None:
    """A server that sends no index keeps parallel calls apart by their place in the delta, and continues each there."""
    calls = [
        {"id": f"call_{city}", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": '}}
        for city in ("paris", "rome")
    ]
    more = {"tool_calls": [{"function": {"arguments": f'"{city}"}}'}} for city in ("Paris", "Rome")]}
    reply = _chat_reply(_chunk({"tool_calls": calls}), _chunk(more, finish_reason="tool_calls"))
    built = _Built(tmp_path, _OPENAI_CHAT, reply)

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert [(call["id"], call["function"]["arguments"]) for call in turn.tool_calls] == [
        ("call_paris", '{"city": "Paris"}'),
        ("call_rome", '{"city": "Rome"}'),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("finish_reason", "stop"), [("length", "output_limit"), ("content_filter", "content_filter")])
async def test_chat_finish_reason_maps_to_stop_reason(tmp_path: Path, finish_reason: str, stop: str) -> None:
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "cut"}, finish_reason=finish_reason)))

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.stop_reason == stop
    assert "finish_reason" not in (turn.message.provider_data or {})


@pytest.mark.asyncio
async def test_a_chat_stream_moves_a_leading_think_block_to_reasoning(tmp_path: Path) -> None:
    """A server without a reasoning parser streams a model's reasoning inside ``<think>``; it never shows as text."""
    reply = _chat_reply(
        _chunk({"content": "<thi"}),
        _chunk({"content": "nk>Rain.</th"}),
        _chunk({"content": "ink>\n\nPack"}),
        _chunk({"content": " a coat."}, finish_reason="stop"),
    )
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, reply)

    events = [event async for event in built.provider.stream(ModelRequest(messages=[_user()]))]

    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert (completed.message.content, completed.message.reasoning_content) == ("Pack a coat.", "Rain.")
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Pack a coat."
    assert "".join(event.text for event in events if isinstance(event, ReasoningDelta)) == "Rain."
    assert "finish_reason" not in (completed.message.provider_data or {})
    assert built.body()["stream"] is True
    assert built.requests[0].headers["authorization"] == "Bearer not-provided"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "content", "expected"),
    [
        ("llama_cpp", "\n<think>Rain.</think>Pack a coat.", ("Pack a coat.", "Rain.")),
        ("llama_cpp", "<think>\n\n</think>Pack a coat.", ("Pack a coat.", None)),
        ("llama_cpp", "Rain.</think>Pack a coat.", ("Rain.</think>Pack a coat.", None)),
        ("llama_cpp", "Pack a coat. <think>Rain.</think>", ("Pack a coat. <think>Rain.</think>", None)),
        ("llama_cpp", "<think>Rain, cut off at </th", (None, "Rain, cut off at </th")),
        ("groq", "<think>Rain.</think>Pack a coat.", ("Pack a coat.", "Rain.")),
        ("cerebras", "<think>Rain.</think>Pack a coat.", ("Pack a coat.", "Rain.")),
    ],
    ids=[
        "leading-whitespace",
        "empty-reasoning-dropped",
        "no-opening-tag-kept",
        "later-tag-kept",
        "unclosed-reasoning",
        "groq",
        "cerebras",
    ],
)
async def test_a_chat_stream_splits_only_a_think_block_that_opens_the_reply(
    tmp_path: Path,
    provider: str,
    content: str,
    expected: tuple[str | None, str | None],
) -> None:
    """Only a reply that opens with ``<think>`` streams reasoning, and a block the reply never closes is all reasoning."""
    reply = _chat_reply(_chunk({"content": content}, finish_reason="stop"))
    built = _Built(tmp_path, {"provider": provider, "id": "qwen", "api_key": "k"}, reply)

    events = [event async for event in built.provider.stream(ModelRequest(messages=[_user()]))]

    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert (completed.message.content, completed.message.reasoning_content) == expected
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    reasoning = "".join(event.text for event in events if isinstance(event, ReasoningDelta))
    assert (text or None, reasoning or None) == expected


@pytest.mark.asyncio
async def test_text_a_chat_stream_holds_until_its_end_is_still_its_first_output(tmp_path: Path) -> None:
    """A reply that could still open a ``<think>`` block shows nothing until the stream ends, then times the turn."""
    reply = _chat_reply(_chunk({"content": "<"}, finish_reason="stop"))
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, reply)

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.content == "<"
    assert turn.usage.time_to_first_token is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["llama_cpp", "groq", "cerebras"])
@pytest.mark.parametrize(
    ("server_id", "stored"),
    [
        (None, _NEW_CHAT_ID),
        ("functions.get_weather:0", _NEW_CHAT_ID),
        (f"call_{'a' * 35}", f"call_{'a' * 35}"),
        (f"call_{'a' * 36}", _NEW_CHAT_ID),
    ],
    ids=["omitted", "unprefixed", "at-40-characters", "past-40-characters"],
)
async def test_a_chat_call_keeps_only_a_server_id_that_chat_requests_send_unchanged(
    tmp_path: Path,
    provider: str,
    server_id: str | None,
    stored: str,
) -> None:
    """A call without an id or with one outside the Chat rule gets a new id that fits it, on every Chat route.

    Chat requests rewrite an id that is not ``call_`` and at most 40 characters, so the call is stored under one they
    send unchanged.
    """
    built = _Built(tmp_path, {"provider": provider, "id": "qwen", "api_key": "k"}, _tool_call_reply(server_id))

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert re.fullmatch(stored, turn.tool_calls[0]["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "cerebras"])
async def test_groq_and_cerebras_streams_record_their_reasoning(tmp_path: Path, provider: str) -> None:
    """The reasoning these routes stream in ``reasoning`` reaches the turn, as on every other Chat route."""
    reply = _chat_reply(
        _chunk({"reasoning": "Rain is likely"}),
        _chunk({"reasoning": " this afternoon."}),
        _chunk({"content": "Pack a coat."}, finish_reason="stop"),
    )
    built = _Built(tmp_path, {"provider": provider, "id": "gpt-oss-120b", "api_key": "k"}, reply)

    events = [event async for event in built.provider.stream(ModelRequest(messages=[_user()]))]

    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert (completed.message.content, completed.message.reasoning_content) == (
        "Pack a coat.",
        "Rain is likely this afternoon.",
    )
    assert [event.text for event in events if isinstance(event, ReasoningDelta)] == [
        "Rain is likely",
        " this afternoon.",
    ]


class _Packing(BaseModel):
    item: str


def _answer_call_reply(arguments: str) -> dict[str, Any]:
    """Return a Chat stream whose one call is the forced ``structured_output`` call with ``arguments``."""
    call = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "structured_output", "arguments": arguments},
    }
    return _chat_reply(_chunk({"tool_calls": [call]}, finish_reason="tool_calls"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_config",
    [
        {"provider": "groq", "id": "openai/gpt-oss-120b", "api_key": "k"},
        {"provider": "deepseek", "id": "deepseek-flash", "api_key": "k", "extra_kwargs": {"use_thinking": False}},
    ],
    ids=["groq", "deepseek-without-thinking"],
)
async def test_a_route_that_can_force_a_tool_asks_for_structured_output_with_a_forced_call(
    tmp_path: Path,
    model_config: dict[str, Any],
) -> None:
    """The output schema goes as the one tool the request forces, streamed, and the call carries the answer."""
    built = _Built(tmp_path, model_config, _answer_call_reply('{"item": "coat"}'))

    turn = await _turn(built, ModelRequest(messages=[_user()], output_schema=_Packing))

    body = built.body()
    assert built.adapter.capabilities.structured_output == "forced_tool"
    assert "response_format" not in body
    assert body["stream"] is True
    assert body["tool_choice"] == {"type": "function", "function": {"name": "structured_output"}}
    assert [tool["function"]["name"] for tool in body["tools"]] == ["structured_output"]
    assert body["tools"][0]["function"]["parameters"] == _Packing.model_json_schema()
    assert [call["function"]["arguments"] for call in turn.tool_calls] == ['{"item": "coat"}']


@pytest.mark.parametrize(
    ("extra_kwargs", "structured_output"),
    [
        ({"use_thinking": False, "extra_body": {"thinking": {"type": "enabled"}}}, "json_mode"),
        ({"request_params": {"extra_body": {"thinking": {"type": "disabled"}}}}, "forced_tool"),
    ],
    ids=["authored-on", "authored-off"],
)
def test_a_deepseek_route_follows_the_thinking_its_requests_send(
    tmp_path: Path,
    extra_kwargs: dict[str, Any],
    structured_output: str,
) -> None:
    """An authored ``extra_body.thinking`` wins over ``use_thinking`` on the wire, so the structured-output mode follows it."""
    config = {"provider": "deepseek", "id": "deepseek-flash", "api_key": "k", "extra_kwargs": extra_kwargs}
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "{}"}, finish_reason="stop")))

    assert built.adapter.capabilities.structured_output == structured_output


@pytest.mark.asyncio
async def test_a_thinking_deepseek_model_asks_for_structured_output_in_json_mode(tmp_path: Path) -> None:
    """DeepSeek rejects a forced tool call in thinking mode, so a thinking model asks for JSON instead."""
    built = _Built(
        tmp_path,
        {"provider": "deepseek", "id": "deepseek-flash", "api_key": "k"},
        _chat_reply(_chunk({"content": '{"item": "coat"}'}, finish_reason="stop")),
    )

    turn = await _turn(built, ModelRequest(messages=[_user()], output_schema=_Packing))

    body = built.body()
    assert built.adapter.capabilities.structured_output == "json_mode"
    assert (body["response_format"], "tools" in body, "tool_choice" in body) == ({"type": "json_object"}, False, False)
    assert turn.content == '{"item": "coat"}'


def _tool_call_reply(server_id: str | None) -> dict[str, Any]:
    """Return a Chat stream with one ``get_weather`` call under ``server_id``, or no id when it is None."""
    call: dict[str, Any] = {"index": 0, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    if server_id is not None:
        call["id"] = server_id
    return _chat_reply(_chunk({"tool_calls": [call]}, finish_reason="tool_calls"))


_COAT = _chunk({"content": "Pack a coat."})
_STOP = _chunk({}, finish_reason="stop")
_DONE = {"data": "[DONE]"}


def _chat_events(*chunks: dict[str, Any]) -> dict[str, Any]:
    return {"status": 200, "headers": {"content-type": "text/event-stream"}, "sse": list(chunks)}


def _streamed_chat(*chunks: dict[str, Any]) -> dict[str, Any]:
    """Return a Chat stream whose body streams as a server's does, so the adapter can tell where it ends."""
    return {**_chat_events(*chunks), "pause_seconds": 0.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "stop_reason"),
    [([_STOP, _DONE], "end"), ([_STOP], "end"), ([_DONE], None)],
    ids=["finish-reason-and-done", "finish-reason-without-done", "done-without-finish-reason"],
)
async def test_a_chat_stream_ends_its_turn_at_a_finish_reason_or_done(
    tmp_path: Path,
    ending: list[dict[str, Any]],
    stop_reason: str | None,
) -> None:
    """OpenAI-compatible servers may omit ``finish_reason`` or ``[DONE]``; either one ends the turn."""
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _streamed_chat(_COAT, *ending))

    turn = await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (turn.content, turn.stop_reason) == ("Pack a coat.", stop_reason)


class _RecordedBody(PacedBytes):
    """A streamed response body that records whether it was closed."""

    closed = False

    async def aclose(self) -> None:
        """Record the close."""
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [[_STOP, _DONE], []], ids=["ended-at-done", "cut-off"])
async def test_a_chat_stream_closes_its_response_body(tmp_path: Path, ending: list[dict[str, Any]]) -> None:
    """The response body is closed once the stream stops, at ``[DONE]`` or cut off, which releases its connection."""
    whole = encoded_response(_chat_events(_COAT, *ending))
    body = _RecordedBody([whole.content])
    adapter = openai_family_adapter(
        {"provider": "llama_cpp", "id": "qwen"},
        tmp_path,
        lambda _request: httpx.Response(200, headers=whole.headers, stream=body),
    )

    [event async for event in adapter.stream(ModelRequest(messages=[_user()]))]

    assert body.closed


@pytest.mark.asyncio
async def test_a_chat_stream_cut_off_without_a_finish_reason_or_done_ends_no_turn(tmp_path: Path) -> None:
    """A body that ends after text without either was cut off: its text arrives, but the turn does not end.

    The request pipeline turns the missing turn into its retryable early-end error.
    """
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _streamed_chat(_COAT))

    events = [event async for event in built.adapter.stream(ModelRequest(messages=[_user()]))]

    assert events == [TextDelta("Pack a coat.")]


@pytest.mark.asyncio
async def test_a_chat_call_id_may_arrive_after_its_name_and_arguments(tmp_path: Path) -> None:
    opening = {"index": 0, "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
    identified = {"index": 0, "id": "call_1", "type": "function"}
    built = _Built(
        tmp_path,
        {"provider": "llama_cpp", "id": "qwen"},
        _chat_reply(
            _chunk({"tool_calls": [opening]}),
            _chunk({"tool_calls": [identified]}, finish_reason="tool_calls"),
        ),
    )

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.tool_calls == [_CALL]


@pytest.mark.asyncio
async def test_an_openrouter_stream_keeps_its_reasoning_details_and_routing_fields(tmp_path: Path) -> None:
    """Reasoning details streamed in pieces are stored whole for replay, with the upstream OpenRouter routed to."""
    first = [{"type": "reasoning.text", "text": "Rain is likely", "index": 0}]
    second = [{"type": "reasoning.text", "text": " this afternoon.", "index": 0}]
    built = _Built(
        tmp_path,
        {"provider": "openrouter", "id": "anthropic/claude-sonnet-5.5", "api_key": "k"},
        _chat_reply(
            _chunk({"reasoning": "Rain is likely", "reasoning_details": first}, provider="Anthropic"),
            _chunk(
                {"content": "Pack a coat.", "reasoning": " this afternoon.", "reasoning_details": second},
                finish_reason="stop",
                provider="Anthropic",
            ),
        ),
    )

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.message.reasoning_content == "Rain is likely this afternoon."
    assert turn.message.provider_data["reasoning_details"] == first + second
    assert turn.message.provider_data["model_extra"] == {"provider": "Anthropic"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "groq", "cerebras"])
async def test_a_chat_request_sends_no_participant_name_and_no_empty_call_list(tmp_path: Path, provider: str) -> None:
    """A stored message's ``name`` is not sent, and an assistant message with no calls sends no ``tool_calls``."""
    history = [
        Message(role="user", content="Hi", name="alice"),
        Message(role="assistant", content="Hello.", tool_calls=[]),
        _user(),
    ]
    model_config = _OPENAI_CHAT if provider == "openai" else _DECISION_ROUTES[provider][0]
    built = _Built(tmp_path, model_config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=history))

    assert built.body()["messages"][:2] == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello."},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "env_file", "system_role"),
    [
        (_OPENAI_CHAT, "", "developer"),
        ({**_OPENAI_CHAT, "extra_kwargs": {"base_url": "https://api.openai.com/v1/"}}, "", "developer"),
        ({**_OPENAI_CHAT, "extra_kwargs": {"base_url": "http://localhost:8000/v1"}}, "", "system"),
        ({**_OPENAI_CHAT, "extra_kwargs": {"client_params": {"base_url": "http://vllm.test/v1"}}}, "", "system"),
        (_OPENAI_CHAT, "OPENAI_BASE_URL=http://127.0.0.1:8080/v1\n", "system"),
        (
            {"provider": "llama_cpp", "id": "qwen", "extra_kwargs": {"base_url": "http://localhost:8080/v1"}},
            "",
            "system",
        ),
    ],
    ids=["openai", "openai-endpoint-set", "base-url", "client-params-base-url", "env-base-url", "llama-cpp"],
)
async def test_only_openai_s_own_api_gets_the_system_prompt_as_developer(
    tmp_path: Path,
    model_config: dict[str, Any],
    env_file: str,
    system_role: str,
) -> None:
    """An OpenAI-compatible server behind ``provider: openai``, such as vLLM or llama.cpp, gets the ``system`` role."""
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    built = _Built(tmp_path, model_config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[Message(role="system", content="Be brief."), _user()]))

    assert built.body()["messages"][0] == {"role": system_role, "content": "Be brief."}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "groq", "cerebras"])
async def test_a_chat_request_sends_stored_calls_without_other_routes_fields(tmp_path: Path, provider: str) -> None:
    """A call another route stored keeps only the Chat call fields, without Gemini's or Responses' replay state."""
    gemini_call = {**_CALL, "id": "call_gemini", "thought_signature": "c2ln"}
    responses_call = {**_CALL, "id": "fc_1", "call_id": "call_resp"}
    history = [
        _user(),
        Message(role="assistant", tool_calls=[gemini_call, responses_call]),
        Message(role="tool", tool_call_id="call_gemini", tool_name="get_weather", content="sunny"),
        Message(role="tool", tool_call_id="fc_1", tool_name="get_weather", content="rainy"),
        _user(),
    ]
    model_config = _OPENAI_CHAT if provider == "openai" else _DECISION_ROUTES[provider][0]
    built = _Built(tmp_path, model_config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=history))

    sent = built.body()["messages"][1]["tool_calls"]
    assert [set(call) for call in sent] == [{"id", "type", "function"}] * 2
    assert [call["function"] for call in sent] == [_CALL["function"]] * 2


@pytest.mark.asyncio
async def test_a_thread_switching_to_a_chat_route_rewrites_foreign_call_ids(tmp_path: Path) -> None:
    """Calls a Responses model made earlier in the thread, and their results, take ids Chat Completions accepts."""
    responses_call = {**_CALL, "id": "fc_68a1", "call_id": "call_68a1"}
    chat_call = {**_CALL, "id": "call_2"}
    # A result may name either id of a Responses call, and a resumed turn repeats the call it continues.
    history = [
        _user(),
        Message(role="assistant", tool_calls=[responses_call, chat_call]),
        Message(role="tool", tool_call_id="fc_68a1", content="sunny"),
        Message(role="tool", tool_call_id="call_2", content="rainy"),
        Message(role="assistant", tool_calls=[responses_call]),
        Message(role="tool", tool_call_id="call_68a1", content="sunny"),
    ]
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=history))

    sent = built.body()["messages"]
    rewritten = "call_" + "0" * 35
    assert [call["id"] for call in sent[1]["tool_calls"]] == [rewritten, "call_2"]
    assert [message["tool_call_id"] for message in sent if message["role"] == "tool"] == [
        rewritten,
        "call_2",
        rewritten,
    ]
    assert sent[4]["tool_calls"][0]["id"] == rewritten
    assert history[1].tool_calls == [responses_call, chat_call]


# --- Chat attachments -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chat_route_sends_audio_and_file_attachments_inline(tmp_path: Path) -> None:
    """Chat Completions carries attachments as inline parts after the text, the audio ahead of the file.

    The file part is named by the attachment's original filename, not its stored path.
    """
    voice = tmp_path / "att_voice.wav"
    voice.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt synthetic")
    report = tmp_path / "att_report.pdf"
    report.write_bytes(b"%PDF-1.7 synthetic")
    message = Message(
        role="user",
        content="Summarize both.",
        audio=[Audio(id="att_voice", filepath=str(voice), mime_type="audio/wav")],
        files=[File(id="att_report", filepath=str(report), mime_type="application/pdf", filename="report.pdf")],
    )
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[message]))

    pdf = base64.b64encode(report.read_bytes()).decode()
    assert built.body()["messages"][0]["content"] == [
        {"type": "text", "text": "Summarize both."},
        {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(voice.read_bytes()).decode(), "format": "wav"},
        },
        {"type": "file", "file": {"filename": "report.pdf", "file_data": f"data:application/pdf;base64,{pdf}"}},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Compare them.", None], ids=["with-text", "without-text"])
async def test_a_chat_route_keeps_file_attachments_in_the_order_they_came(tmp_path: Path, text: str | None) -> None:
    """Each file keeps its place among the message's files, and a message without text still carries them."""
    files = [File(content=f"%PDF-{name}".encode(), filename=f"{name}.pdf") for name in ("first", "second", "third")]
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[Message(role="user", content=text, files=files)]))

    content = built.body()["messages"][0]["content"]
    assert [part["file"]["filename"] for part in content if part["type"] == "file"] == [
        "first.pdf",
        "second.pdf",
        "third.pdf",
    ]
    assert [part["text"] for part in content if part["type"] == "text"] == ([text] if text else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply"),
    [
        (_OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop"))),
        (_OPENAI_RESPONSES, _responses_reply(*_text_events("ok"), _completed())),
    ],
    ids=["chat", "responses"],
)
async def test_attachments_deleted_since_their_turn_are_sent_as_the_media_note(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
) -> None:
    """A turn whose attachment files were cleaned up still runs: its text is sent with the media fallback's note.

    The first attempt finds the photo gone and the retry without it the report, so neither attempt reaches the
    provider, and the request goes once without any of the media.
    """
    gone = tmp_path / "cleaned-up"
    photo, report = gone / "att_photo.png", gone / "att_report.pdf"
    earlier = Message(
        role="user",
        content="Compare these.",
        images=[Image(id="att_photo", filepath=str(photo), mime_type="image/png")],
        files=[File(id="att_report", filepath=str(report), mime_type="application/pdf")],
    )
    built = _Built(tmp_path, model_config, reply)

    with capture_logs() as logs:
        await _turn(built, ModelRequest(messages=[earlier, _user("Which is newer?")]))

    body = built.body()
    sent = body["messages"][0] if "messages" in body else body["input"][0]
    assert sent["content"] == f"Compare these.\n\n{FALLBACK_MARKER}\n{INLINE_MEDIA_FALLBACK_PROMPT}"
    assert [log["path"] for log in logs if log["event"] == "media_file_unreadable"] == [str(photo), str(report)]
    assert len(built.requests) == 1


# --- Errors ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type", "retryable"),
    [
        (400, {"error": {"message": "too long", "code": "context_length_exceeded"}}, ContextWindowExceeded, False),
        (400, {"error": {"message": "Unknown parameter: 'x'", "code": "unknown_parameter"}}, InvalidRequest, False),
        (429, {"error": {"message": "slow down"}}, RateLimited, True),
        (503, {"error": {"message": "overloaded"}}, ProviderUnavailable, True),
        (401, {"error": {"message": "bad key"}}, AuthenticationFailed, False),
    ],
)
async def test_http_errors_map_to_typed_provider_errors(
    tmp_path: Path,
    status: int,
    body: dict[str, Any],
    error_type: type[ProviderError],
    retryable: bool,
) -> None:
    built = _Built(tmp_path, _OPENAI_CHAT, _json_reply(body, status=status))

    with pytest.raises(ProviderError) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert type(info.value) is error_type
    assert (info.value.status_code, info.value.retryable, info.value.provider) == (status, retryable, "OpenAI")
    assert str(info.value) == body["error"]["message"]
    assert len(built.requests) == 1, "the SDK client never retries; the pipeline alone does"


@pytest.mark.asyncio
async def test_a_retry_after_date_reaches_the_pipeline_as_the_seconds_until_it(tmp_path: Path) -> None:
    """A custom endpoint that answers a 429 with an HTTP-date ``Retry-After`` is waited out as the SDK waited it out."""
    retry_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)
    reply = _json_reply({"error": {"message": "slow down"}}, status=429)
    reply["headers"]["retry-after"] = format_datetime(retry_at, usegmt=True)
    config = {"provider": "llama_cpp", "id": "qwen", "extra_kwargs": {"base_url": "https://llm.example/v1"}}
    built = _Built(tmp_path, config, reply)

    with pytest.raises(RateLimited) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert info.value.retry_after_seconds is not None
    assert 25.0 < info.value.retry_after_seconds <= 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [520, 522, 524])
async def test_a_cdn_origin_failure_in_front_of_a_custom_endpoint_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A Cloudflare origin failure is a server error the request pipeline retries, as the SDK did before it."""
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    replies = [
        _json_reply({"error": {"message": "A timeout occurred"}}, status=status),
        _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
    ]
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx.Response:
        sent.append(request)
        return encoded_response(replies[len(sent) - 1])

    config = {"provider": "llama_cpp", "id": "qwen", "extra_kwargs": {"base_url": "https://llm.example/v1"}}
    provider = pipeline_provider(openai_family_adapter(config, tmp_path, respond))

    turn = await complete(provider, ModelRequest(messages=[_user()]))

    assert (turn.content, len(sent)) == ("ok", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "header", "value", "seconds"),
    [
        (_OPENAI_CHAT, "retry-after", "2", 2.0),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "api_key": "test-key",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com"},
            },
            "retry-after-ms",
            "30000",
            30.0,
        ),
    ],
    ids=["openai-seconds", "azure-milliseconds"],
)
async def test_a_retry_after_header_rides_on_the_typed_error(
    tmp_path: Path,
    model_config: dict[str, Any],
    header: str,
    value: str,
    seconds: float,
) -> None:
    """The provider's ``Retry-After`` hint, Azure's ``retry-after-ms`` too, reaches the pipeline, which waits it out."""
    reply = _json_reply({"error": {"message": "slow down"}}, status=429)
    reply["headers"][header] = value
    built = _Built(tmp_path, model_config, reply)

    with pytest.raises(RateLimited) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.retry_after_seconds, len(built.requests)) == (seconds, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        ({"message": "Overloaded", "type": "overloaded_error"}, True),
        ({"message": "Slow down", "code": 529}, True),
        ({"message": "Timed out", "code": "408"}, True),
        ({"message": "Bad input", "code": "400"}, False),
    ],
    ids=["overloaded-type", "overloaded-status", "transient-status", "client-status"],
)
async def test_mid_stream_error_events_are_retryable_when_transient(
    tmp_path: Path,
    error: dict[str, Any],
    retryable: bool,
) -> None:
    """An error event inside a committed stream is retried for the shared rate-limit and transient statuses."""
    reply = _chat_reply({"data": {"error": error}})
    built = _Built(tmp_path, _OPENAI_CHAT, reply)

    with pytest.raises(ProviderError) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert info.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "error_type", "user_message"),
    [
        (
            {"message": "Rate limit exceeded: free-models-per-min", "code": 429},
            RateLimited,
            "⏱️ Rate limited. Please wait a moment and try again.",
        ),
        (
            {"message": "Slow down", "code": 529},
            ProviderUnavailable,
            "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly.",
        ),
        (
            {"message": "Service Unavailable", "code": 503},
            ProviderUnavailable,
            "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly.",
        ),
    ],
    ids=["429", "529", "503"],
)
async def test_a_stream_error_event_reads_as_the_status_its_code_names(
    tmp_path: Path,
    error: dict[str, Any],
    error_type: type[ProviderError],
    user_message: str,
) -> None:
    """A stream's error event reads as the HTTP status its code names does: 429 rate-limited, 529 and 503 unavailable."""
    reply = _chat_reply({"data": {"error": error}})
    built = _Built(tmp_path, _OPENAI_CHAT, reply)

    with pytest.raises(ProviderError) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert get_user_friendly_error_message(info.value) == user_message
    assert type(info.value) is error_type


@pytest.mark.asyncio
async def test_a_context_window_error_inside_a_stream_is_typed(tmp_path: Path) -> None:
    """A server that reports an overlong request after the stream started still triggers history compaction."""
    error = {
        "message": "This model's maximum context length is 131072 tokens.",
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _chat_reply({"data": {"error": error}}))

    with pytest.raises(ContextWindowExceeded, match="maximum context length") as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.provider_code, info.value.retryable) == ("context_length_exceeded", False)


@pytest.mark.asyncio
async def test_a_stream_error_event_whose_error_is_text_reads_as_that_text(tmp_path: Path) -> None:
    """An error event that carries its error as a bare string is classified by the string, not the SDK's placeholder."""
    error = "This model's maximum context length is 131072 tokens."
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _chat_reply({"data": {"error": error}}))

    with pytest.raises(ContextWindowExceeded, match="maximum context length"):
        await _adapter_turn(built, ModelRequest(messages=[_user()]))


@pytest.mark.asyncio
async def test_an_error_body_without_a_message_reports_the_response_text(tmp_path: Path) -> None:
    """Servers that answer outside the OpenAI error shape still surface what they said."""
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _json_reply({"detail": "Not Found"}, status=404))
    request = ModelRequest(messages=[_user()], overrides=RequestOverrides(caller_retries=True))

    with pytest.raises(InvalidRequest) as info:
        await _turn(built, request)

    assert (str(info.value), info.value.status_code) == ('{"detail": "Not Found"}', 404)


def test_an_sdk_failure_without_text_is_named_by_its_type() -> None:
    """A failure the SDK raises without text reports its type name, so the reply says what failed."""
    error = provider_error(RuntimeError(), provider="OpenAI Chat")

    assert (type(error), str(error)) == (ProviderError, "RuntimeError")


def test_a_proxy_error_page_becomes_a_retryable_error_with_its_text() -> None:
    """A proxy in front of the endpoint answers 502 with an HTML page, which the SDK keeps as the error body."""
    page = "<html><body>502 Bad Gateway</body></html>"
    response = httpx.Response(502, text=page, request=httpx.Request("POST", "https://proxy.test/v1/chat/completions"))

    error = provider_error(openai.InternalServerError(page, response=response, body=page), provider="OpenAI Chat")

    assert type(error) is ProviderUnavailable
    assert (str(error), error.status_code, error.retryable) == (page, 502, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "variable", "reply", "authorization"),
    [
        (
            {"provider": "codex", "id": "gpt-6.1-sol"},
            "CODEX_HOME",
            _responses_reply(*_text_events("ok"), _completed()),
            "Bearer eyJ",
        ),
        (
            {"provider": "kimi", "id": "k3"},
            "KIMI_CODE_HOME",
            _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
            "Bearer parity-kimi-token",
        ),
    ],
    ids=["codex", "kimi"],
)
@pytest.mark.parametrize("source", ["env-file", "env-file-under-blank-export", "authored-wins"])
async def test_a_cli_login_home_set_in_the_env_file_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_config: dict[str, Any],
    variable: str,
    reply: dict[str, Any],
    authorization: str,
    source: str,
) -> None:
    """``CODEX_HOME`` and ``KIMI_CODE_HOME`` in the ``.env`` beside the config locate the CLI's saved login.

    A blank export is unset, a home the model authors wins, and the user's own home holds no login, so neither test
    can find one there.
    """
    authored = source == "authored-wins"
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    homes = {"CODEX_HOME": ("<codex_home>", "codex_home"), "KIMI_CODE_HOME": ("<kimi_home>", "kimi_home")}
    placeholder, setting = homes[variable]
    login_home = write_cli_logins(tmp_path)[placeholder]
    if authored:
        model_config = {**model_config, "extra_kwargs": {setting: login_home}}
    env_home = tmp_path / "no-login" if authored else login_home
    (tmp_path / ".env").write_text(f"{variable}={env_home}\n", encoding="utf-8")
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx.Response:
        sent.append(request)
        return encoded_response(reply)

    exported = {variable: ""} if source == "env-file-under-blank-export" else {}
    runtime = replace(
        _runtime(tmp_path, **exported),
        http_client=httpx2.AsyncClient(transport=httpx2_transport(respond)),
    )

    await complete(build_provider(ModelConfig(**model_config), runtime), ModelRequest(messages=[_user()]))

    assert sent[0].headers["authorization"].startswith(authorization)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "variable", "placeholder"),
    [
        ({"provider": "openai", "id": "gpt-4.1"}, "OPENAI_API_KEY", "your-openai-key-here"),
        ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, "OPENROUTER_API_KEY", "your-openrouter-key-here"),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com"},
            },
            "AZURE_OPENAI_API_KEY",
            "your-azure-openai-key-here",
        ),
    ],
    ids=["openai", "openrouter", "azure"],
)
async def test_a_starter_placeholder_key_is_a_missing_key(
    tmp_path: Path,
    model_config: dict[str, Any],
    variable: str,
    placeholder: str,
) -> None:
    """An unedited starter ``.env`` value is no key, so the reply says how to connect one instead of sending it."""
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})

    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={variable: placeholder},
    )
    runtime = provider_runtime(paths, http_client=httpx2.AsyncClient(transport=httpx2_transport(respond)))
    adapter = build_provider(ModelConfig(**model_config), runtime)

    with pytest.raises(AuthenticationFailed) as info:
        await complete(adapter, ModelRequest(messages=[_user()]))

    assert (info.value.code, sent) == ("missing_api_key", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply", "message", "provider"),
    [
        ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, _chat_reply(), "OPENROUTER_API_KEY not set", "OpenRouter"),
        (
            {"provider": "openai", "api": "responses", "id": "gpt-6-astra"},
            _responses_reply(),
            "OPENAI_API_KEY not set",
            "OpenAI",
        ),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com"},
            },
            _chat_reply(),
            "Please provide one of: AZURE_OPENAI_API_KEY environment variable, azure_ad_token, or",
            "Azure",
        ),
    ],
    ids=["chat", "responses", "azure"],
)
async def test_missing_api_key_fails_at_the_first_request(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
    message: str,
    provider: str,
) -> None:
    """The error names the route's provider, as every other error of the route does."""
    built = _Built(tmp_path, model_config, reply)

    with pytest.raises(ProviderError, match=message) as info:
        await _turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, built.requests) == (401, "missing_api_key", [])
    assert info.value.provider == provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "home_key", "reply", "message"),
    [
        ({"provider": "kimi_code", "id": "k3"}, "kimi_home", _chat_reply(), "Kimi Code credentials not found"),
        ({"provider": "codex", "id": "gpt-6.1-sol"}, "codex_home", _responses_reply(), "Codex auth file not found"),
    ],
    ids=["kimi", "codex"],
)
async def test_a_missing_cli_login_fails_the_request_before_sending(
    tmp_path: Path,
    model_config: dict[str, Any],
    home_key: str,
    reply: dict[str, Any],
    message: str,
) -> None:
    """Without a CLI login the request fails as an authentication error the user fixes by logging in."""
    logged_out = tmp_path / "logged-out"
    logged_out.mkdir()
    built = _Built(tmp_path, {**model_config, "extra_kwargs": {home_key: str(logged_out)}}, reply)

    with pytest.raises(AuthenticationFailed, match=message) as info:
        await _turn(built, ModelRequest(messages=[_user()]))

    assert info.value.status_code == 401
    assert built.requests == []


@pytest.mark.asyncio
async def test_a_codex_login_without_an_account_sends_no_account_header(tmp_path: Path) -> None:
    """A ChatGPT login without an account id still works; the account header is simply left out."""
    codex_home = tmp_path / "login"
    codex_home.mkdir()
    tokens = {"access_token": unsigned_jwt({"exp": 4102444800}), "refresh_token": "refresh-value"}
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8")
    model_config = {"provider": "codex", "id": "gpt-6.1-sol", "extra_kwargs": {"codex_home": str(codex_home)}}
    built = _Built(tmp_path, model_config, _responses_reply(*_text_events("ok"), _completed()))

    await _turn(built, ModelRequest(messages=[_user()]))

    headers = built.requests[0].headers
    assert headers["authorization"] == f"Bearer {tokens['access_token']}"
    assert "chatgpt-account-id" not in headers


@pytest.mark.asyncio
async def test_a_codex_request_sends_no_temperature_override_on_any_model(tmp_path: Path) -> None:
    """Codex rejects a temperature, so even a model outside the default-sampling families sends none."""
    codex_home = tmp_path / "login"
    codex_home.mkdir()
    tokens = {"access_token": unsigned_jwt({"exp": 4102444800}), "refresh_token": "refresh-value"}
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8")
    model_config = {"provider": "codex", "id": "o4-mini", "extra_kwargs": {"codex_home": str(codex_home)}}
    built = _Built(tmp_path, model_config, _responses_reply(*_text_events("ok"), _completed()))

    await _turn(built, ModelRequest(messages=[_user()], overrides=RequestOverrides(temperature=0.2)))

    assert "temperature" not in built.body()


# --- Responses lifecycle --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_error_event_is_classified_by_its_code(tmp_path: Path) -> None:
    """The event's code names the failure; the event has no HTTP status of its own."""
    error = {"type": "error", "sequence_number": 1, "code": "server_error", "message": "boom", "param": None}
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(_text_events("")[0], error))

    with pytest.raises(ProviderUnavailable) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.provider_code, str(info.value)) == (None, "server_error", "boom")
    assert info.value.provider == "OpenAI"


@pytest.mark.asyncio
async def test_failed_response_reports_its_usage_before_raising(tmp_path: Path) -> None:
    failed = {
        "type": "response.failed",
        "sequence_number": 2,
        "response": _response("failed", error={"code": "context_length_exceeded", "message": "too long"}),
    }
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(_text_events("")[0], failed))
    events: list[Any] = []

    async def drain() -> None:
        async for event in built.adapter.stream(ModelRequest(messages=[_user()])):
            events.append(event)  # noqa: PERF401 - the events before the raise are the subject

    with pytest.raises(ContextWindowExceeded):
        await drain()

    assert [event.usage.tokens.input_tokens for event in events if isinstance(event, UsageReported)] == [42]


_REASONING_ITEM = {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Rain is likely."}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "requests"), [(_text_events("")[:1], 5), (_text_events("Pack"), 1)])
async def test_responses_stream_without_completion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, Any]],
    requests: int,
) -> None:
    """An early end is retried like a dropped connection only while nothing reached the caller."""
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(*events))

    with pytest.raises(ProviderUnavailable, match="without completing the turn"):
        await _turn(built, ModelRequest(messages=[_user()]))

    assert len(built.requests) == requests


_SEARCH_CALL = {"id": "ws_1", "type": "web_search_call", "status": "in_progress", "action": {"type": "search"}}
_FUNCTION_CALL = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "get_weather",
    "arguments": "",
    "status": "in_progress",
}
_SERVER_ERROR = {"type": "error", "sequence_number": 8, "code": "server_error", "message": "boom", "param": None}


def _item_event(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": f"response.output_item.{kind}", "sequence_number": 1, "output_index": 0, "item": item}


def _argument_delta(delta: str) -> dict[str, Any]:
    return {
        "type": "response.function_call_arguments.delta",
        "sequence_number": 2,
        "item_id": "fc_1",
        "output_index": 0,
        "delta": delta,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "drop_connection", "requests"),
    [
        ([_text_events("")[0], _SERVER_ERROR], False, 5),
        (
            [
                _text_events("")[0],
                _item_event("added", _SEARCH_CALL),
                _item_event("done", {**_SEARCH_CALL, "status": "completed"}),
                _SERVER_ERROR,
            ],
            False,
            1,
        ),
        ([_text_events("")[0], _item_event("added", _FUNCTION_CALL), _argument_delta('{"city"')], True, 5),
        (
            [
                _text_events("")[0],
                _item_event("added", _REASONING_ITEM),
                _item_event("done", _REASONING_ITEM),
                _SERVER_ERROR,
            ],
            False,
            5,
        ),
    ],
    ids=[
        "before-output",
        "hosted-tool-then-error-event",
        "function-call-then-dropped-connection",
        "finished-reasoning-then-error-event",
    ],
)
async def test_responses_failure_is_final_once_a_hosted_tool_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, Any]],
    drop_connection: bool,
    requests: int,
) -> None:
    """A retry after a hosted tool started could run the tool again, so the failure stands.

    A function call still streaming its arguments is the model's unfinished output, which a retry writes again.
    """
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    reply = {**_responses_reply(*events), "drop_connection": drop_connection}
    built = _Built(tmp_path, _OPENAI_RESPONSES, reply)

    with pytest.raises(ProviderUnavailable):
        await _turn(built, ModelRequest(messages=[_user()]))

    assert len(built.requests) == requests


_IDLE_LIMIT_SECONDS = 0.5
# Each pause stays far below the idle limit while the whole stream outlasts it.
_EVENT_PAUSE_SECONDS = 0.04
_SILENT_EVENTS = 25


def _chat_tool_argument_stream() -> dict[str, Any]:
    opening = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
    pieces = [_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "x"}}]}) for _ in range(_SILENT_EVENTS)]
    return _chat_reply(_chunk({"tool_calls": [opening]}), *pieces, _chunk({}, finish_reason="tool_calls"))


def _responses_hosted_tool_stream() -> dict[str, Any]:
    searching = {
        "type": "response.web_search_call.searching",
        "sequence_number": 2,
        "item_id": "ws_1",
        "output_index": 0,
    }
    return _responses_reply(
        _text_events("")[0],
        _item_event("added", _SEARCH_CALL),
        *[searching] * _SILENT_EVENTS,
        _item_event("done", {**_SEARCH_CALL, "status": "completed"}),
        _completed(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply", "stop_reason"),
    [
        (_OPENAI_CHAT, _chat_tool_argument_stream(), "tool_use"),
        (_OPENAI_RESPONSES, _responses_hosted_tool_stream(), "end"),
    ],
    ids=["chat-tool-arguments", "responses-hosted-tool"],
)
async def test_provider_events_without_output_keep_a_stream_inside_the_idle_limit(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
    stop_reason: str,
) -> None:
    """The idle limit measures the gap between provider events, including those that carry no output."""
    paced = {**reply, "pause_seconds": _EVENT_PAUSE_SECONDS}
    built = _Built(tmp_path, model_config, paced, idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert (turn.stop_reason, len(built.requests)) == (stop_reason, 1)


@pytest.mark.asyncio
async def test_native_compaction_replays_without_storage_and_records_its_route(tmp_path: Path) -> None:
    output = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Pack", "annotations": []}],
        },
    ]
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(*_text_events("Pack"), _completed(output)))
    native = NativeCompactionReplay(route="route-1", threshold=50_000)

    turn = await _turn(built, ModelRequest(messages=[_user()], native_compaction=native))

    body = built.body()
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["context_management"] == [{"type": "compaction", "compact_threshold": 50_000}]
    assert turn.message.provider_data["mindroom_native_compaction"] == {
        "route": "route-1",
        "threshold": 50_000,
        "checkpoint_prefix": False,
    }
    assert turn.message.provider_data["mindroom_response_stored"] is False


_UNSTORED_OUTPUT = [
    {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
    {"id": "cmp_1", "type": "compaction", "encrypted_content": "checkpoint"},
    {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Pack", "annotations": []}],
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [True, False], ids=["stored", "unstored"])
@pytest.mark.parametrize("listed", [True, False], ids=["listed-by-the-ended-response", "only-streamed"])
async def test_a_response_keeps_its_output_for_replay_without_its_compaction_item(
    tmp_path: Path,
    *,
    listed: bool,
    store: bool,
) -> None:
    """A reply keeps its output for replay, from the ended response or, when that lists none, its stream.

    It does on every route, whatever the route stores, and the compaction item stays out of that output.
    """
    done = [
        {"type": "response.output_item.done", "sequence_number": 2 + index, "output_index": index, "item": item}
        for index, item in enumerate(_UNSTORED_OUTPUT)
    ]
    ended = _completed(_UNSTORED_OUTPUT if listed else None)
    reply = _responses_reply(*_text_events("Pack"), *done, ended)
    built = _Built(tmp_path, {**_OPENAI_RESPONSES, "extra_kwargs": {"store": store}}, reply)

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    reasoning, _compaction, message = _UNSTORED_OUTPUT
    assert turn.message.provider_data[RESPONSE_OUTPUT_KEY] == [reasoning, message]


def test_responses_native_compaction_settings_match_the_claude_contract(tmp_path: Path) -> None:
    """The Responses adapter answers the history planner like the Claude one: a route hash, or None without support."""
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(_completed()))
    adapter = built.adapter
    assert isinstance(adapter, ResponsesAdapter)

    settings = adapter.native_compaction_settings(threshold=60_000, history_generation="gen-2", allow_authored=False)

    assert settings == NativeCompactionReplay(
        route=native_compaction_route(
            provider="OpenAI",
            model_id="gpt-6-astra",
            endpoint="https://api.openai.com/v1",
            history_generation="gen-2",
        ),
        threshold=60_000,
    )
    assert adapter.native_compaction_settings(threshold=None, history_generation="", allow_authored=True) is None
    (tmp_path / "proxy").mkdir()
    proxied = _Built(
        tmp_path / "proxy",
        {**_OPENAI_RESPONSES, "extra_kwargs": {"base_url": "https://proxy.example/v1"}},
        _responses_reply(_completed()),
    ).adapter
    assert isinstance(proxied, ResponsesAdapter)
    assert proxied.native_compaction_settings(threshold=60_000, history_generation="", allow_authored=False) is None


# --- Request settings -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authored", "temperature"),
    [
        ({}, 0.2),
        ({"temperature": 0.5}, 0.2),
        ({"request_params": {"temperature": 0.7}}, 0.7),
        ({"extra_body": {"temperature": 0.9}}, 0.9),
    ],
    ids=["override", "over-configured", "request_params-wins", "extra_body-wins"],
)
async def test_a_temperature_override_is_sent_as_the_models_temperature(
    tmp_path: Path,
    authored: dict[str, Any],
    temperature: float,
) -> None:
    """An override replaces the model's temperature, so it sits where the SDK puts its ``temperature`` keyword.

    Agno set the override on the model, so authored ``request_params`` and ``extra_body`` still win over it.
    """
    built = _Built(
        tmp_path,
        {**_OPENAI_CHAT, "id": "gpt-4.1", "extra_kwargs": authored},
        _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
    )

    tool = ToolSpec(
        name="get_weather",
        description="Forecast.",
        parameters={"type": "object"},
        strict=None,
    )

    await _turn(built, ModelRequest(messages=[_user()], tools=[tool], overrides=RequestOverrides(temperature=0.2)))

    body = built.body()
    assert list(body) == ["model", "stream", "messages", "stream_options", "temperature", "tools"]
    assert body["temperature"] == temperature


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_config",
    [_OPENAI_CHAT, {"provider": "openrouter", "id": "anthropic/claude-sonnet-5.5", "api_key": "test-key"}],
    ids=["gpt-6", "claude-5.5-through-openrouter"],
)
async def test_temperature_override_is_left_to_default_sampling_models(
    tmp_path: Path,
    model_config: dict[str, Any],
) -> None:
    built = _Built(tmp_path, model_config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()], overrides=RequestOverrides(temperature=0.2)))

    assert "temperature" not in built.body()


_DECISION_TOOL = ToolSpec(
    name="get_weather",
    description="Forecast.",
    parameters={"type": "object"},
    strict=None,
)
_DECISION_ROUTES = {
    "openai-chat": (_OPENAI_CHAT, "text"),
    "openai-responses": (_OPENAI_RESPONSES, "responses"),
    "cerebras": ({"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "k"}, "text"),
    "groq": ({"provider": "groq", "id": "openai/gpt-oss-120b", "api_key": "k"}, "text"),
    "openrouter": ({"provider": "openrouter", "id": "z-ai/glm-5.3", "api_key": "k"}, "text"),
}


def _decision_reply(kind: str) -> dict[str, Any]:
    if kind == "responses":
        return _responses_reply(*_text_events("{}"), _completed())
    return _chat_reply(_chunk({"content": "{}"}, finish_reason="stop"))


async def _regular_and_decision_bodies(
    tmp_path: Path,
    model_config: dict[str, Any],
    request: ModelRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send ``request`` once as a reply and once as a decision (``tool_choice`` "none"), returning both wire bodies."""
    reply_kind = "responses" if model_config.get("api") == "responses" else "text"
    built = _Built(tmp_path, model_config, _decision_reply(reply_kind))
    await _turn(built, request)
    await _turn(built, replace(request, tool_choice="none"))
    return built.body(0), built.body(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_DECISION_ROUTES))
@pytest.mark.parametrize("source", ["unset", "request_params", "extra_body"])
async def test_decisions_disable_tool_selection_after_authored_overrides(
    tmp_path: Path,
    route: str,
    source: str,
) -> None:
    """A decision never selects a function, whatever the config authored, and keeps the reply's tool prefix."""
    model_config = deepcopy(_DECISION_ROUTES[route][0])
    if source == "request_params":
        model_config["extra_kwargs"] = {"request_params": {"tool_choice": "required"}}
    elif source == "extra_body":
        model_config["extra_kwargs"] = {"request_params": {"extra_body": {"tool_choice": "required"}}}
    authored = deepcopy(model_config)

    regular, decision = await _regular_and_decision_bodies(
        tmp_path,
        model_config,
        ModelRequest(messages=[_user()], tools=[_DECISION_TOOL]),
    )

    assert decision["tool_choice"] == "none"
    assert regular.get("tool_choice") == (None if source == "unset" else "required")
    assert decision["tools"] == regular["tools"]
    assert model_config == authored


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", ["web_search", "code_interpreter", "mcp", "file_search"])
async def test_responses_decisions_disable_authored_hosted_tools(tmp_path: Path, tool_type: str) -> None:
    """Authored hosted tools stay in the request but cannot run, because the decision selects no tool."""
    model_config = {**_OPENAI_RESPONSES, "extra_kwargs": {"request_params": {"tools": [{"type": tool_type}]}}}

    regular, decision = await _regular_and_decision_bodies(tmp_path, model_config, ModelRequest(messages=[_user()]))

    assert decision["tool_choice"] == "none"
    assert decision["tools"] == regular["tools"] == [{"type": tool_type}]


@pytest.mark.asyncio
async def test_responses_decisions_keep_the_deferred_tool_search_prefix(tmp_path: Path) -> None:
    request = ModelRequest(
        messages=[_user()],
        tools=[_DECISION_TOOL],
        deferred_tools=DeferredTools(names=frozenset({"get_weather"})),
    )

    regular, decision = await _regular_and_decision_bodies(tmp_path, _OPENAI_RESPONSES, request)

    assert decision["tool_choice"] == "none"
    assert decision["tools"] == regular["tools"]
    assert decision["tools"][0] == {"type": "tool_search"}
    assert decision["tools"][1]["defer_loading"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "error"),
    [
        ({**_OPENAI_CHAT, "id": "gpt-6-search-preview"}, "native Chat Completions search"),
        ({**_OPENAI_CHAT, "id": "gpt-5-search-api"}, "native Chat Completions search"),
        (
            {**_OPENAI_CHAT, "extra_kwargs": {"request_params": {"web_search_options": {}}}},
            "native Chat Completions search",
        ),
        (
            {**_OPENAI_CHAT, "extra_kwargs": {"request_params": {"extra_body": {"web_search_options": {}}}}},
            "native Chat Completions search",
        ),
        ({**_DECISION_ROUTES["openrouter"][0], "id": "openai/gpt-6-astra:online"}, "OpenRouter search"),
        (
            {**_DECISION_ROUTES["openrouter"][0], "extra_kwargs": {"models": ["openai/gpt-6-astra:online"]}},
            "OpenRouter search",
        ),
        (
            {**_DECISION_ROUTES["openrouter"][0], "extra_kwargs": {"request_params": {"plugins": [{"id": "web"}]}}},
            "OpenRouter search",
        ),
        ({**_DECISION_ROUTES["groq"][0], "id": "groq/compound"}, "Groq Compound"),
        ({**_DECISION_ROUTES["groq"][0], "id": "compound-beta-mini"}, "Groq Compound"),
        (
            {
                **_DECISION_ROUTES["groq"][0],
                "extra_kwargs": {"request_params": {"compound_custom": {"tools": {"enabled_tools": ["web_search"]}}}},
            },
            "Groq Compound",
        ),
        (
            {
                **_DECISION_ROUTES["groq"][0],
                "extra_kwargs": {"request_params": {"extra_body": {"model": "groq/compound"}}},
            },
            "Groq Compound",
        ),
    ],
    ids=[
        "chat-search-preview",
        "chat-search-api",
        "chat-web-search-options",
        "chat-extra-body-web-search-options",
        "openrouter-online-model",
        "openrouter-online-fallback-model",
        "openrouter-web-plugin",
        "groq-compound",
        "groq-compound-beta-mini",
        "groq-compound-custom",
        "groq-extra-body-compound-model",
    ],
)
async def test_decisions_fail_before_sending_on_routes_whose_hosted_search_always_runs(
    tmp_path: Path,
    model_config: dict[str, Any],
    error: str,
) -> None:
    built = _Built(tmp_path, model_config, _chat_reply())

    with pytest.raises(ValueError, match=error):
        await _turn(built, ModelRequest(messages=[_user()], tools=[_DECISION_TOOL], tool_choice="none"))

    assert built.requests == []


@pytest.mark.asyncio
async def test_a_disabled_openrouter_web_plugin_still_lets_decisions_run_without_tools(tmp_path: Path) -> None:
    plugins = [{"id": "web", "enabled": False}, {"id": "response-healing"}]
    model_config = {**_DECISION_ROUTES["openrouter"][0], "extra_kwargs": {"request_params": {"plugins": plugins}}}

    regular, decision = await _regular_and_decision_bodies(
        tmp_path,
        model_config,
        ModelRequest(messages=[_user()], tools=[_DECISION_TOOL]),
    )

    assert decision == {**regular, "tool_choice": "none"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "use_thinking", "thinking"),
    [
        ("deepseek-flash", {"use_thinking": False}, {"type": "disabled"}),
        ("deepseek-v4-pro", {}, {"type": "enabled"}),
        ("deepseek-chat", {}, {"type": "enabled"}),
    ],
    ids=["turned-off", "default", "any-model-id"],
)
async def test_deepseek_thinking_follows_use_thinking(
    tmp_path: Path,
    model_id: str,
    use_thinking: dict[str, bool],
    thinking: dict[str, str],
) -> None:
    """Every DeepSeek model thinks unless the config turns it off, and without thinking no reasoning effort is sent."""
    config = {
        "provider": "deepseek",
        "id": model_id,
        "api_key": "k",
        "extra_kwargs": {**use_thinking, "reasoning_effort": "max"},
    }
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    body = built.body()
    assert body.get("thinking") == thinking
    assert ("reasoning_effort" in body) is (thinking["type"] == "enabled")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "parallel"),
    [("llama-4-scout-17b-16e-instruct", False), ("gpt-oss-120b", True)],
    ids=["serial-tool-model", "configured"],
)
async def test_cerebras_parallel_tool_calls_follow_the_model_then_the_config(
    tmp_path: Path,
    model_id: str,
    parallel: bool,
) -> None:
    """The model Cerebras serves only with serial calls never gets parallel ones, whatever the config says."""
    config = {"provider": "cerebras", "id": model_id, "api_key": "k", "extra_kwargs": {"parallel_tool_calls": True}}
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()], tools=[_DECISION_TOOL]))

    assert built.body()["parallel_tool_calls"] is parallel


class _Entity(BaseModel):
    name: str
    kind: str


class _Extraction(BaseModel):
    entities: list[_Entity]
    summary: str


@pytest.mark.asyncio
async def test_cerebras_structured_output_closes_every_object(tmp_path: Path) -> None:
    """Cerebras accepts a JSON schema only when every object, the nested models under ``$defs`` included, is closed."""
    built = _Built(
        tmp_path,
        {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "k"},
        _chat_reply(_chunk({"content": "{}"}, finish_reason="stop")),
    )
    await _turn(built, ModelRequest(messages=[_user()], output_schema=_Extraction))

    response_format = built.body()["response_format"]
    sent = response_format["json_schema"]
    # The format keeps the order the helper always sent: ``type`` first, the properties in field order.
    assert (list(response_format), list(sent)) == (["type", "json_schema"], ["name", "schema", "strict"])
    assert list(sent["schema"]["$defs"]["_Entity"]["properties"]) == list(_Entity.model_fields) == ["name", "kind"]
    assert sent["strict"] is True
    assert sent["schema"]["additionalProperties"] is False
    assert sent["schema"]["$defs"]["_Entity"]["additionalProperties"] is False
    assert sent["schema"]["properties"]["entities"]["items"] == {"$ref": "#/$defs/_Entity"}
    assert "additionalProperties" not in _Extraction.model_json_schema()


@pytest.mark.asyncio
async def test_responses_reasoning_summary_and_verbosity_reach_the_body(tmp_path: Path) -> None:
    extra_kwargs = {"reasoning_effort": "high", "reasoning_summary": "auto", "verbosity": "low"}
    built = _Built(
        tmp_path,
        {**_OPENAI_RESPONSES, "extra_kwargs": extra_kwargs},
        _responses_reply(*_text_events("ok"), _completed()),
    )

    await _turn(built, ModelRequest(messages=[_user()]))

    body = built.body()
    assert (body["reasoning"], body["text"]) == ({"effort": "high", "summary": "auto"}, {"verbosity": "low"})


_PACK = "Pack light."
_PACK_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": _PACK, "annotations": []}],
}
_PACK_REASONING = {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Think. "}]}
_PACK_SUMMARY_DELTA = {
    "type": "response.reasoning_summary_text.delta",
    "sequence_number": 1,
    "item_id": "rs_1",
    "output_index": 0,
    "summary_index": 0,
    "delta": "Think. ",
}


def _reasoning_reply(*, summary: bool) -> dict[str, Any]:
    output = [_PACK_REASONING, _PACK_MESSAGE] if summary else [_PACK_MESSAGE]
    created, text = _text_events(_PACK)
    return _responses_reply(created, *([_PACK_SUMMARY_DELTA] if summary else []), text, _completed(output))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_kwargs", "summary", "stored"),
    [
        ({}, False, None),
        ({"reasoning": {"effort": "low"}}, False, None),
        ({"reasoning": {"effort": "low", "summary": "auto"}}, True, "Think. "),
        ({"reasoning": {"effort": "low"}, "reasoning_summary": "auto"}, True, "Think. "),
    ],
    ids=["unset", "authored", "authored-summary-in-reasoning", "reasoning-summary"],
)
async def test_a_response_stores_only_the_reasoning_summary_it_returns_as_reasoning(
    tmp_path: Path,
    *,
    extra_kwargs: dict[str, Any],
    summary: bool,
    stored: str | None,
) -> None:
    """Authored ``reasoning`` settings never make the output text a reply's reasoning, streamed or stored."""
    built = _Built(tmp_path, {**_OPENAI_RESPONSES, "extra_kwargs": extra_kwargs}, _reasoning_reply(summary=summary))

    events = [event async for event in built.adapter.stream(ModelRequest(messages=[_user()]))]

    deltas = [event for event in events if isinstance(event, (ReasoningDelta, TextDelta))]
    assert deltas == [*([ReasoningDelta("Think. ")] if summary else []), TextDelta(_PACK)]
    [turn] = [event for event in events if isinstance(event, TurnCompleted)]
    assert (turn.message.content, turn.message.reasoning_content) == (_PACK, stored)


def test_a_reasoning_summary_carries_when_the_model_began_reasoning() -> None:
    """A summary streams seconds after the model began reasoning, so its deltas carry when the reasoning item began."""
    stream = ResponsesStream(
        provider="openai",
        store=None,
        portable_replay=True,
        native_compaction=None,
        conversation_digest="",
    )
    reasoning_item = {"id": "rs_1", "type": "reasoning", "summary": []}
    stream.add(
        ResponseOutputItemAddedEvent.model_validate(
            {"type": "response.output_item.added", "sequence_number": 0, "output_index": 0, "item": reasoning_item},
        ),
    )
    summary_arrives = time.monotonic()

    [delta] = stream.add(ResponseReasoningSummaryTextDeltaEvent.model_validate(_PACK_SUMMARY_DELTA))

    assert isinstance(delta, ReasoningDelta)
    assert delta.text == "Think. "
    assert delta.started_at is not None
    assert delta.started_at < summary_arrives


def test_each_reasoning_summary_part_starts_a_paragraph() -> None:
    """Summary parts each open with a bold title; without a break, two titles run together as one garbled line."""
    stream = ResponsesStream(
        provider="codex",
        store=False,
        portable_replay=True,
        native_compaction=None,
        conversation_digest="",
    )
    parts = [("rs_1", 0, "**Comparing options**"), ("rs_1", 1, "**Refining the plan**"), ("rs_1", 1, " Trains win.")]

    deltas = [
        delta.text
        for index, (item_id, summary_index, text) in enumerate(parts)
        for delta in stream.add(
            ResponseReasoningSummaryTextDeltaEvent.model_validate(
                {
                    **_PACK_SUMMARY_DELTA,
                    "sequence_number": index,
                    "item_id": item_id,
                    "summary_index": summary_index,
                    "delta": text,
                },
            ),
        )
    ]

    assert deltas == ["**Comparing options**", "\n\n**Refining the plan**", " Trains win."]


@pytest.mark.asyncio
async def test_responses_tool_parameters_keep_a_list_of_types(tmp_path: Path) -> None:
    """An MCP tool may declare a nullable parameter as a list of types; Responses receives the list as authored."""
    tool = ToolSpec(
        name="search_issues",
        description="Search issues.",
        parameters={
            "type": "object",
            "properties": {"label": {"type": ["string", "null"]}, "limit": {"type": "integer"}},
        },
        strict=None,
    )
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(*_text_events("ok"), _completed()))

    await _turn(built, ModelRequest(messages=[_user()], tools=[tool]))

    sent = built.body()["tools"][0]["parameters"]["properties"]
    assert sent == {"label": {"type": ["string", "null"]}, "limit": {"type": "integer"}}
    assert tool.parameters["properties"]["label"]["type"] == ["string", "null"]


@pytest.mark.asyncio
async def test_configured_groq_host_keeps_the_sdk_path(tmp_path: Path) -> None:
    config = {
        "provider": "groq",
        "id": "openai/gpt-oss-120b",
        "api_key": "k",
        "extra_kwargs": {"base_url": "http://groq.test"},
    }
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    assert str(built.requests[0].url) == "http://groq.test/openai/v1/chat/completions"


def _client_options(prefix: str) -> dict[str, Any]:
    return {"timeout": 30, "default_headers": {"X-Origin": prefix}, "organization": f"org-{prefix}"}


_CONFIGURED_CLIENT = {"base_url": "http://configured.test/v1", **_client_options("configured")}
_CLIENT_PARAMS = {"base_url": "http://client.test/v1", **_client_options("client_params")}
_AZURE_CLIENT = {
    "azure_endpoint": "https://configured.openai.azure.com",
    "api_version": "2024-10-21",
    "default_headers": {"X-Origin": "configured"},
    "organization": "org-configured",
}


@pytest.mark.asyncio
async def test_client_options_set_in_extra_kwargs_reach_the_client(tmp_path: Path) -> None:
    """The endpoint, headers, and organization a Chat model sets directly in ``extra_kwargs`` reach the SDK client."""
    built = _Built(
        tmp_path,
        {**_OPENAI_CHAT, "extra_kwargs": _CONFIGURED_CLIENT},
        _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
    )

    await _turn(built, ModelRequest(messages=[_user()]))

    request = built.requests[0]
    assert (str(request.url), request.headers["x-origin"], request.headers["openai-organization"]) == (
        "http://configured.test/v1/chat/completions",
        "configured",
        "org-configured",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply", "url", "endpoint"),
    [
        (
            {**_OPENAI_CHAT, "extra_kwargs": {**_CONFIGURED_CLIENT, "client_params": _CLIENT_PARAMS}},
            _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
            "http://client.test/v1/chat/completions",
            "http://client.test/v1",
        ),
        (
            {**_OPENAI_RESPONSES, "extra_kwargs": {**_CONFIGURED_CLIENT, "client_params": _CLIENT_PARAMS}},
            _responses_reply(*_text_events("ok"), _completed()),
            "http://client.test/v1/responses",
            "http://client.test/v1",
        ),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "api_key": "test-key",
                "extra_kwargs": {
                    **_AZURE_CLIENT,
                    "client_params": {
                        "azure_endpoint": "https://client.openai.azure.com",
                        "api_version": "2025-04-01-preview",
                        "default_headers": {"X-Origin": "client_params"},
                        "organization": "org-client_params",
                    },
                },
            },
            _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
            "https://client.openai.azure.com/openai/deployments/gpt-6-astra/chat/completions"
            "?api-version=2025-04-01-preview",
            "https://client.openai.azure.com",
        ),
    ],
    ids=["chat", "responses", "azure"],
)
async def test_client_params_override_the_configured_client_options(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
    url: str,
    endpoint: str,
) -> None:
    """``client_params`` win over the same options set directly in ``extra_kwargs``, endpoint included."""
    built = _Built(tmp_path, model_config, reply)

    await _turn(built, ModelRequest(messages=[_user()]))

    request = built.requests[0]
    assert (str(request.url), request.headers["x-origin"], request.headers["openai-organization"]) == (
        url,
        "client_params",
        "org-client_params",
    )
    assert built.provider.identity.endpoint == endpoint


@pytest.mark.asyncio
async def test_a_client_params_key_needs_no_shared_key(tmp_path: Path) -> None:
    """The client sends ``client_params.api_key`` over every other key, so the shared one may be unset."""
    model_config = {
        "provider": "openai",
        "id": "gpt-4.1",
        "api": "chat_completions",
        "extra_kwargs": {"client_params": {"base_url": "http://proxy.test/v1", "api_key": "proxy-key"}},
    }
    built = _Built(tmp_path, model_config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    assert built.requests[0].headers["authorization"] == "Bearer proxy-key"


@pytest.mark.asyncio
async def test_an_azure_model_sends_once_with_its_configured_timeout(tmp_path: Path) -> None:
    """``timeout`` set in ``extra_kwargs`` reaches the Azure client, which never retries, as on the other routes."""
    model_config = {
        "provider": "azure",
        "id": "gpt-6-astra",
        "api_key": "test-key",
        "extra_kwargs": {**_AZURE_CLIENT, "timeout": 30, "client_params": {"max_retries": 2}},
    }
    built = _Built(tmp_path, model_config, _json_reply({"error": {"message": "down"}}, status=500))

    with pytest.raises(ProviderError):
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert len(built.requests) == 1
    assert built.requests[0].extensions["timeout"]["read"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_base_url", "host"),
    [("http://client.test", "http://client.test"), (None, "http://groq.test"), ("", "http://groq.test")],
)
async def test_client_params_base_url_is_the_groq_host_when_set(
    tmp_path: Path,
    client_base_url: str | None,
    host: str,
) -> None:
    config = {
        "provider": "groq",
        "id": "openai/gpt-oss-120b",
        "api_key": "k",
        "extra_kwargs": {"base_url": "http://groq.test", "client_params": {"base_url": client_base_url}},
    }
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    assert str(built.requests[0].url) == f"{host}/openai/v1/chat/completions"
    assert built.provider.identity.endpoint == f"{host}/openai/v1"


@pytest.mark.asyncio
async def test_kimi_borrows_the_login_token_for_every_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    borrowed: list[str | None] = []

    def borrow(*, kimi_home: str | None) -> str:
        borrowed.append(kimi_home)
        return f"token-{len(borrowed)}"

    monkeypatch.setattr(openai_build, "borrow_kimi_token", borrow)
    built = _Built(
        tmp_path,
        {"provider": "kimi_code", "id": "kimi-code/k3"},
        _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
    )

    await _turn(built, ModelRequest(messages=[_user()]))
    await _turn(built, ModelRequest(messages=[_user()]))

    assert len(borrowed) == 2
    assert [request.headers["authorization"] for request in built.requests] == ["Bearer token-1", "Bearer token-2"]
    assert built.body()["model"] == "k3"


def test_unknown_extra_kwargs_are_rejected_with_the_valid_keys(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(ValueError, match="Unsupported extra_kwargs for provider 'openai': reasoning_efort") as info:
        build_provider(ModelConfig(**{**_OPENAI_RESPONSES, "extra_kwargs": {"reasoning_efort": "high"}}), runtime)

    assert "reasoning_effort" in str(info.value)
    with pytest.raises(ValueError, match="extra_body"):
        build_provider(ModelConfig(provider="groq", id="m", extra_kwargs={"extra_body": {}}), runtime)


# --- Routing and identity -------------------------------------------------------------------------------------


def _runtime(tmp_path: Path, **env: str) -> ProviderRuntime:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env=env)
    return provider_runtime(paths)


@pytest.mark.parametrize(
    ("config", "provider", "endpoint"),
    [
        ({"provider": "openai", "id": "gpt-4.1"}, "OpenAI", "https://api.openai.com/v1"),
        ({"provider": "openai", "id": "gpt-6-astra"}, "OpenAI", "https://api.openai.com/v1"),
        (
            {"provider": "codex", "id": "gpt-6.1-sol"},
            "OpenAI Codex",
            "https://chatgpt.com/backend-api/codex",
        ),
        (
            {"provider": "openai-codex", "id": "gpt-6.1-sol"},
            "OpenAI Codex",
            "https://chatgpt.com/backend-api/codex",
        ),
        ({"provider": "kimi_code", "id": "k3"}, "Kimi", "https://api.kimi.com/coding/v1"),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com/"},
            },
            "Azure",
            "https://r.openai.azure.com",
        ),
        ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, "OpenRouter", "https://openrouter.ai/api/v1"),
        ({"provider": "deepseek", "id": "deepseek-flash"}, "DeepSeek", "https://api.deepseek.com"),
        ({"provider": "zai", "id": "glm-5.3"}, "ZAI", "https://api.z.ai/api/paas/v4"),
        ({"provider": "llama_cpp", "id": "qwen"}, "LlamaCpp", "http://127.0.0.1:8080/v1"),
        ({"provider": "groq", "id": "openai/gpt-oss-120b"}, "Groq", "https://api.groq.com/openai/v1"),
        ({"provider": "Cerebras", "id": "gpt-oss-120b"}, "Cerebras", "https://api.cerebras.ai/v1"),
    ],
)
def test_identity_strings_match_the_stored_provider_names(
    tmp_path: Path,
    config: dict[str, Any],
    provider: str,
    endpoint: str,
) -> None:
    identity = build_provider(ModelConfig(**config), _runtime(tmp_path)).identity

    assert (identity.provider, identity.endpoint) == (provider, endpoint)


def test_openai_routes_pick_responses_only_where_it_is_selected_or_searchable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    proxied = _runtime(tmp_path, OPENAI_BASE_URL="http://localhost:4000/v1")
    gpt6 = ModelConfig(provider="openai", id="gpt-6-astra")

    assert isinstance(build_provider(gpt6, runtime), ResponsesAdapter)
    assert isinstance(build_provider(gpt6, proxied), ChatAdapter)
    client_proxied = ModelConfig(
        provider="openai",
        id="gpt-6-astra",
        extra_kwargs={"client_params": {"base_url": "http://localhost:4000/v1"}},
    )
    assert isinstance(build_provider(client_proxied, runtime), ChatAdapter)
    assert isinstance(
        build_provider(ModelConfig(provider="openai", id="gpt-6-astra", api="chat_completions"), runtime),
        ChatAdapter,
    )
    assert isinstance(
        build_provider(ModelConfig(provider="openai", id="gpt-4.1", api="responses"), runtime),
        ResponsesAdapter,
    )


def test_responses_capabilities_follow_model_and_endpoint(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    gpt6 = build_provider(ModelConfig(provider="openai", id="gpt-6-astra"), runtime).capabilities
    gpt5 = build_provider(ModelConfig(provider="openai", id="gpt-5", api="responses"), runtime).capabilities
    stored = build_provider(
        ModelConfig(provider="openai", id="gpt-6-astra", extra_kwargs={"store": True}),
        runtime,
    ).capabilities
    proxied = build_provider(
        ModelConfig(
            provider="openai",
            id="gpt-6-astra",
            api="responses",
            extra_kwargs={"client_params": {"base_url": "http://localhost:4000/v1"}},
        ),
        runtime,
    ).capabilities

    assert (gpt6.native_tool_search, gpt6.native_compaction) == (True, True)
    assert (gpt5.native_tool_search, gpt5.native_compaction) == (False, False)
    assert stored.native_compaction is False
    assert (proxied.native_tool_search, proxied.native_compaction) == (False, False)
    assert gpt6.omitted_media == frozenset({"audio", "video"})


def test_non_openai_family_provider_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an OpenAI-family provider"):
        build_provider(ModelConfig(provider="anthropic", id="claude-sonnet-5-5"), _runtime(tmp_path))
