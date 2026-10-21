"""OpenAI-family adapter behavior the wire goldens do not reach: legacy history, errors, overrides, routing."""
# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import ContextWindowExceeded, InvalidRequest, ProviderError, ProviderUnavailable, RateLimited
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    ModelRequest,
    NativeCompactionReplay,
    RequestOverrides,
    ToolSpec,
    UsageReported,
    complete,
)
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.openai import build as openai_build
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.tool_policy import without_provider_tools
from tests.providers.openai_family_support import golden_provider

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

_USAGE = {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}
_RESPONSE_USAGE = {"input_tokens": 42, "output_tokens": 12, "total_tokens": 54}
_OPENAI_CHAT = {"provider": "openai", "api": "chat_completions", "id": "gpt-6-astra", "api_key": "test-key"}
_OPENAI_RESPONSES = {"provider": "openai", "api": "responses", "id": "gpt-6-astra", "api_key": "test-key"}
_CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}


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


# --- Legacy history -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combined_gemini_tool_results_split_into_canonical_results(tmp_path: Path) -> None:
    calls = [_CALL, {**_CALL, "id": "call_2"}]
    combined = Message(
        role="tool",
        content=["sunny", "rainy"],
        tool_calls=[
            {"tool_call_id": "call_1", "tool_name": "get_weather", "content": "sunny"},
            {"tool_call_id": "call_2", "tool_name": "get_weather", "content": "rainy"},
        ],
    )
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user(), Message(role="assistant", tool_calls=calls), combined]))

    tool_messages = [message for message in built.body()["messages"] if message["role"] == "tool"]
    assert tool_messages == [
        {"role": "tool", "content": "sunny", "tool_call_id": "call_1"},
        {"role": "tool", "content": "rainy", "tool_call_id": "call_2"},
    ]


@pytest.mark.asyncio
async def test_legacy_tool_replay_supplies_missing_arguments(tmp_path: Path) -> None:
    call = {"id": "call_1", "type": "function", "function": {"name": "list_rooms"}}
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))
    history = [
        _user(),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id="call_1", content="[]"),
    ]

    await _turn(built, ModelRequest(messages=history))

    assert built.body()["messages"][1]["tool_calls"][0]["function"] == {"name": "list_rooms", "arguments": "{}"}
    assert "arguments" not in call["function"]


@pytest.mark.asyncio
async def test_legacy_tool_replay_removes_placeholders_and_orphans(tmp_path: Path) -> None:
    history = [
        _user(),
        Message(role="assistant", tool_calls=[{"id": "call_0"}, _CALL]),
        Message(role="tool", tool_call_id="call_0", content="orphan"),
        Message(role="tool", tool_call_id="call_1", content="sunny"),
    ]
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=history))

    sent = built.body()["messages"]
    assert [message.get("tool_call_id") for message in sent if message["role"] == "tool"] == ["call_1"]
    assert sent[1]["tool_calls"] == [_CALL]


# --- Chat parsing ---------------------------------------------------------------------------------------------


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
@pytest.mark.parametrize(("finish_reason", "stop"), [("length", "output_limit"), ("content_filter", "content_filter")])
async def test_chat_finish_reason_maps_to_stop_reason(tmp_path: Path, finish_reason: str, stop: str) -> None:
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "cut"}, finish_reason=finish_reason)))

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert turn.stop_reason == stop
    assert "finish_reason" not in (turn.message.provider_data or {})


@pytest.mark.asyncio
async def test_chat_completion_splits_think_tags_and_records_finish_reason(tmp_path: Path) -> None:
    completion = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "<think>Rain.</think>Pack a coat."},
            },
        ],
        "usage": _USAGE,
    }
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _json_reply(completion))

    turn = await _turn(built, ModelRequest(messages=[_user()], stream=False))

    assert (turn.content, turn.message.reasoning_content) == ("Pack a coat.", "Rain.")
    assert turn.message.provider_data["finish_reason"] == "stop"
    assert "stream" not in built.body()
    assert built.requests[0].headers["authorization"] == "Bearer not-provided"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<think>\n\n</think>Pack a coat.", ("<think>\n\n</think>Pack a coat.", None)),
        ("Rain.</think>Pack a coat.", ("Pack a coat.", "Rain.")),
    ],
    ids=["empty-reasoning-kept", "no-opening-tag-split"],
)
async def test_chat_completion_splits_think_tags_as_agno_did(
    tmp_path: Path,
    content: str,
    expected: tuple[str, str | None],
) -> None:
    """Agno's Chat split at a closing tag alone but kept a reply whose reasoning was empty unchanged."""
    completion = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": _USAGE,
    }
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _json_reply(completion))

    turn = await _turn(built, ModelRequest(messages=[_user()], stream=False))

    assert (turn.content, turn.message.reasoning_content) == expected


@pytest.mark.asyncio
async def test_chat_tool_calls_without_ids_get_generated_ids(tmp_path: Path) -> None:
    delta = {"tool_calls": [{"index": 0, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]}
    built = _Built(
        tmp_path,
        {"provider": "llama_cpp", "id": "qwen"},
        _chat_reply(_chunk(delta, finish_reason="tool_calls")),
    )

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert len(turn.tool_calls[0]["id"]) == 36


# --- Errors ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type", "retryable"),
    [
        (400, {"error": {"message": "too long", "code": "context_length_exceeded"}}, ContextWindowExceeded, False),
        (400, {"error": {"message": "Unknown parameter: 'x'", "code": "unknown_parameter"}}, InvalidRequest, False),
        (429, {"error": {"message": "slow down"}}, RateLimited, True),
        (503, {"error": {"message": "overloaded"}}, ProviderUnavailable, True),
        (401, {"error": {"message": "bad key"}}, ProviderError, False),
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
    # A non-streaming request skips the stream retry stage, so the adapter's own error reaches the caller.
    request = ModelRequest(messages=[_user()], stream=False, overrides=RequestOverrides(sdk_max_retries=0))

    with pytest.raises(ProviderError) as info:
        await _turn(built, request)

    assert type(info.value) is error_type
    assert (info.value.status_code, info.value.retryable, info.value.provider) == (status, retryable, "OpenAI Chat")
    assert str(info.value) == body["error"]["message"]
    assert len(built.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        ({"message": "Overloaded", "type": "overloaded_error"}, True),
        ({"message": "Slow down", "code": 529}, True),
        ({"message": "Timed out", "code": "408"}, True),
        ({"message": "Bad input", "code": "400"}, False),
    ],
    ids=["overloaded-type", "rate-limit-status", "transient-status", "client-status"],
)
async def test_mid_stream_error_events_are_retryable_when_transient(
    tmp_path: Path,
    error: dict[str, Any],
    retryable: bool,
) -> None:
    """An error event inside a committed stream is retried for the shared rate-limit and transient statuses."""
    reply = _chat_reply({"data": {"error": error}})
    built = _Built(tmp_path, {**_OPENAI_CHAT, "extra_kwargs": {"max_retries": 0}}, reply)

    with pytest.raises(ProviderError) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert info.value.retryable is retryable


@pytest.mark.asyncio
async def test_missing_api_key_fails_at_the_first_request(tmp_path: Path) -> None:
    built = _Built(tmp_path, {"provider": "openrouter", "id": "z-ai/glm-5.3"}, _chat_reply())

    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY not set") as info:
        await _turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, built.requests) == (401, "missing_api_key", [])


# --- Responses lifecycle --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_error_event_maps_code_to_status(tmp_path: Path) -> None:
    error = {"type": "error", "sequence_number": 1, "code": "server_error", "message": "boom", "param": None}
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(_text_events("")[0], error))

    with pytest.raises(ProviderUnavailable) as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, str(info.value)) == (500, "server_error", "boom")


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

    assert [event.usage.input_tokens for event in events if isinstance(event, UsageReported)] == [42]


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "retryable"), [(_text_events("")[:1], True), (_text_events("Pack"), False)])
async def test_responses_stream_without_completion_fails(
    tmp_path: Path,
    events: list[dict[str, Any]],
    retryable: bool,
) -> None:
    """An early end is retried like a dropped connection only while nothing reached the caller."""
    built = _Built(tmp_path, {**_OPENAI_RESPONSES, "extra_kwargs": {"max_retries": 0}}, _responses_reply(*events))

    with pytest.raises(ProviderError, match=r"without response\.completed") as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.code, info.value.retryable) == ("incomplete_stream", retryable)


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
    ("events", "drop_connection", "retryable", "requests"),
    [
        ([_text_events("")[0], _SERVER_ERROR], False, True, 5),
        (
            [
                _text_events("")[0],
                _item_event("added", _SEARCH_CALL),
                _item_event("done", {**_SEARCH_CALL, "status": "completed"}),
                _SERVER_ERROR,
            ],
            False,
            False,
            1,
        ),
        ([_text_events("")[0], _item_event("added", _FUNCTION_CALL), _argument_delta('{"city"')], True, False, 1),
    ],
    ids=["before-output", "hosted-tool-then-error-event", "function-call-then-dropped-connection"],
)
async def test_responses_failure_after_exposed_output_is_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, Any]],
    drop_connection: bool,
    retryable: bool,
    requests: int,
) -> None:
    """A retry after a hosted tool or function call started could run the tool again, so the failure stands."""
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    reply = {**_responses_reply(*events), "drop_connection": drop_connection}
    built = _Built(tmp_path, {**_OPENAI_RESPONSES, "extra_kwargs": {"max_retries": 0}}, reply)

    with pytest.raises(ProviderError) as info:
        await _turn(built, ModelRequest(messages=[_user()], native_tools=[{"type": "web_search"}]))

    assert (info.value.retryable, len(built.requests)) == (retryable, requests)
    assert (info.value.code == "incomplete_stream") is not retryable


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


def test_responses_native_compaction_settings_match_the_claude_contract(tmp_path: Path) -> None:
    """The Responses adapter answers the history planner like the Claude one: a route hash, or None without support."""
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(_completed()))
    adapter = built.adapter
    assert isinstance(adapter, ResponsesAdapter)

    settings = adapter.native_compaction_settings(threshold=60_000, history_generation="gen-2")

    assert settings == NativeCompactionReplay(
        route=native_compaction_route(
            provider="OpenAI",
            model_id="gpt-6-astra",
            endpoint="https://api.openai.com/v1",
            history_generation="gen-2",
        ),
        threshold=60_000,
    )
    assert adapter.native_compaction_settings(threshold=None, allow_authored=True) is None
    (tmp_path / "proxy").mkdir()
    proxied = _Built(
        tmp_path / "proxy",
        {**_OPENAI_RESPONSES, "extra_kwargs": {"base_url": "https://proxy.example/v1"}},
        _responses_reply(_completed()),
    ).adapter
    assert isinstance(proxied, ResponsesAdapter)
    assert proxied.native_compaction_settings(threshold=60_000) is None


# --- Request settings -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_overrides_reach_the_body(tmp_path: Path) -> None:
    built = _Built(
        tmp_path,
        {**_OPENAI_CHAT, "id": "gpt-4.1"},
        _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")),
    )

    await _turn(
        built,
        ModelRequest(messages=[_user()], overrides=RequestOverrides(temperature=0.2, max_output_tokens=64)),
    )

    body = built.body()
    assert (body["temperature"], body["max_completion_tokens"]) == (0.2, 64)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "configured", "sent"),
    [
        ({**_OPENAI_CHAT, "id": "gpt-4.1", "extra_kwargs": {"max_tokens": 10}}, "max_tokens", "max_completion_tokens"),
        (
            {
                "provider": "openrouter",
                "id": "z-ai/glm-5.3",
                "api_key": "k",
                "extra_kwargs": {"max_completion_tokens": 10},
            },
            "max_completion_tokens",
            "max_tokens",
        ),
    ],
    ids=["openai", "openrouter"],
)
async def test_output_limit_override_replaces_the_configured_limit_field(
    tmp_path: Path,
    config: dict[str, Any],
    configured: str,
    sent: str,
) -> None:
    """A request never carries both limit names: the override replaces whichever the config set."""
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()], overrides=RequestOverrides(max_output_tokens=64)))

    body = built.body()
    assert body[sent] == 64
    assert configured not in body


@pytest.mark.asyncio
async def test_temperature_override_is_left_to_default_sampling_models(tmp_path: Path) -> None:
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()], overrides=RequestOverrides(temperature=0.2)))

    assert "temperature" not in built.body()


@pytest.mark.asyncio
async def test_decisions_without_tools_disable_tool_selection(tmp_path: Path) -> None:
    built = _Built(tmp_path, _OPENAI_CHAT, _chat_reply(_chunk({"content": "{}"}, finish_reason="stop")))
    tool = ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object"})

    with without_provider_tools():
        await _turn(built, ModelRequest(messages=[_user()], tools=[tool]))

    assert built.body()["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_decisions_reject_native_search_models(tmp_path: Path) -> None:
    built = _Built(tmp_path, {**_OPENAI_CHAT, "id": "gpt-6-search-preview"}, _chat_reply())

    with without_provider_tools(), pytest.raises(ValueError, match="native Chat Completions search"):
        await _turn(built, ModelRequest(messages=[_user()]))


@pytest.mark.asyncio
async def test_deepseek_thinking_follows_use_thinking(tmp_path: Path) -> None:
    config = {
        "provider": "deepseek",
        "id": "deepseek-flash",
        "api_key": "k",
        "extra_kwargs": {"use_thinking": False, "reasoning_effort": "max"},
    }
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    body = built.body()
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


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
    return {"timeout": 30, "max_retries": 0, "default_headers": {"X-Origin": prefix}, "organization": f"org-{prefix}"}


_CONFIGURED_CLIENT = {"base_url": "http://configured.test/v1", **_client_options("configured")}
_CLIENT_PARAMS = {"base_url": "http://client.test/v1", **_client_options("client_params")}
_AZURE_CLIENT = {
    "azure_endpoint": "https://configured.openai.azure.com",
    "api_version": "2024-10-21",
    "default_headers": {"X-Origin": "configured"},
    "organization": "org-configured",
}


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
async def test_client_params_base_url_is_the_groq_host(tmp_path: Path) -> None:
    config = {
        "provider": "groq",
        "id": "openai/gpt-oss-120b",
        "api_key": "k",
        "extra_kwargs": {"base_url": "http://groq.test", "client_params": {"base_url": "http://client.test"}},
    }
    built = _Built(tmp_path, config, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")))

    await _turn(built, ModelRequest(messages=[_user()]))

    assert str(built.requests[0].url) == "http://client.test/openai/v1/chat/completions"
    assert built.provider.identity.endpoint == "http://client.test/openai/v1"


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

    with pytest.raises(ValueError, match="Unsupported extra_kwargs for provider 'openai': background") as info:
        build_provider(ModelConfig(**{**_OPENAI_RESPONSES, "extra_kwargs": {"background": True}}), runtime)

    assert "reasoning_effort" in str(info.value)
    with pytest.raises(ValueError, match="extra_body"):
        build_provider(ModelConfig(provider="groq", id="m", extra_kwargs={"extra_body": {}}), runtime)


# --- Routing and identity -------------------------------------------------------------------------------------


def _runtime(tmp_path: Path, **env: str) -> ProviderRuntime:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env=env)
    return ProviderRuntime(runtime_paths=paths)


@pytest.mark.parametrize(
    ("config", "provider", "provider_name", "endpoint"),
    [
        ({"provider": "openai", "id": "gpt-4.1"}, "OpenAI", "OpenAI Chat", "https://api.openai.com/v1"),
        ({"provider": "openai", "id": "gpt-6-astra"}, "OpenAI", "OpenAI Responses", "https://api.openai.com/v1"),
        (
            {"provider": "codex", "id": "gpt-6.1-sol"},
            "OpenAI Codex",
            "OpenAI Codex Responses",
            "https://chatgpt.com/backend-api/codex",
        ),
        (
            {"provider": "openai-codex", "id": "gpt-6.1-sol"},
            "OpenAI Codex",
            "OpenAI Codex Responses",
            "https://chatgpt.com/backend-api/codex",
        ),
        ({"provider": "kimi_code", "id": "k3"}, "Kimi", "Kimi Chat", "https://api.kimi.com/coding/v1"),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com/"},
            },
            "Azure",
            "Azure",
            "https://r.openai.azure.com",
        ),
        ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, "OpenRouter", "OpenRouter", "https://openrouter.ai/api/v1"),
        ({"provider": "deepseek", "id": "deepseek-flash"}, "DeepSeek", "DeepSeek", "https://api.deepseek.com"),
        ({"provider": "zai", "id": "glm-5.3"}, "ZAI", "ZAI", "https://api.z.ai/api/paas/v4"),
        ({"provider": "llama_cpp", "id": "qwen"}, "LlamaCpp", "LlamaCpp", "http://127.0.0.1:8080/v1"),
        ({"provider": "groq", "id": "openai/gpt-oss-120b"}, "Groq", "Groq", "https://api.groq.com/openai/v1"),
        ({"provider": "Cerebras", "id": "gpt-oss-120b"}, "Cerebras", "Cerebras", "https://api.cerebras.ai/v1"),
    ],
)
def test_identity_strings_match_the_stored_provider_names(
    tmp_path: Path,
    config: dict[str, Any],
    provider: str,
    provider_name: str,
    endpoint: str,
) -> None:
    identity = build_provider(ModelConfig(**config), _runtime(tmp_path)).identity

    assert (identity.provider, identity.provider_name, identity.endpoint) == (provider, provider_name, endpoint)


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

    assert (gpt6.native_tool_search, gpt6.native_compaction, gpt6.approval_receipt_after_response_id) == (
        True,
        True,
        True,
    )
    assert (gpt5.native_tool_search, gpt5.native_compaction) == (False, False)
    assert stored.native_compaction is False
    assert (proxied.native_tool_search, proxied.native_compaction) == (False, False)
    assert gpt6.omitted_media == frozenset({"audio", "video"})


def test_non_openai_family_provider_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an OpenAI-family provider"):
        build_provider(ModelConfig(provider="anthropic", id="claude-sonnet-5-5"), _runtime(tmp_path))
