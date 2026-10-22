"""OpenAI-family adapter behavior the wire goldens do not reach: legacy history, errors, overrides, routing."""
# ruff: noqa: D103

from __future__ import annotations

import base64
import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
import openai
import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.structured import select_response_format
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import ContextWindowExceeded, InvalidRequest, ProviderError, ProviderUnavailable, RateLimited
from mindroom.llm.media import Audio, File, Image
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
from mindroom.providers.openai.errors import provider_error
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


@pytest.mark.asyncio
async def test_a_chat_stream_without_a_finish_reason_reports_no_stop_reason(tmp_path: Path) -> None:
    """OpenAI-compatible servers may end a stream without ``finish_reason``; the text still arrives whole."""
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _chat_reply(_chunk({"content": "Pack a coat."})))

    turn = await _turn(built, ModelRequest(messages=[_user()]))

    assert (turn.content, turn.stop_reason) == ("Pack a coat.", None)


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


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": _USAGE,
    }


_REASONING_DETAILS = [{"type": "reasoning.text", "text": "Rain is likely."}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reasoning_fields", "reasoning_details"),
    [
        (
            {"provider": "deepseek", "id": "deepseek-flash", "api_key": "k"},
            {"reasoning_content": "Rain is likely."},
            None,
        ),
        (
            {"provider": "openrouter", "id": "z-ai/glm-5.3", "api_key": "k"},
            {"reasoning": "Rain is likely.", "reasoning_details": _REASONING_DETAILS},
            _REASONING_DETAILS,
        ),
    ],
    ids=["deepseek", "openrouter"],
)
async def test_a_non_streamed_chat_reply_keeps_its_reasoning(
    tmp_path: Path,
    model_config: dict[str, Any],
    reasoning_fields: dict[str, Any],
    reasoning_details: list[dict[str, str]] | None,
) -> None:
    """Helper and decision requests do not stream; the reasoning those replies carry is kept like a streamed one."""
    built = _Built(tmp_path, model_config, _json_reply(_completion({"content": "Pack a coat.", **reasoning_fields})))

    turn = await _turn(built, ModelRequest(messages=[_user()], stream=False))

    assert (turn.content, turn.message.reasoning_content) == ("Pack a coat.", "Rain is likely.")
    assert turn.message.provider_data.get("reasoning_details") == reasoning_details


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
    """Chat Completions carries attachments as inline parts, the file ahead of the text and the audio after it."""
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
        {"type": "file", "file": {"filename": "att_report.pdf", "file_data": f"data:application/pdf;base64,{pdf}"}},
        {"type": "text", "text": "Summarize both."},
        {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(voice.read_bytes()).decode(), "format": "wav"},
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply", "text_part"),
    [
        (_OPENAI_CHAT, _chat_reply(_chunk({"content": "ok"}, finish_reason="stop")), "text"),
        (_OPENAI_RESPONSES, _responses_reply(*_text_events("ok"), _completed()), "input_text"),
    ],
    ids=["chat", "responses"],
)
async def test_attachments_deleted_since_their_turn_are_left_out(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
    text_part: str,
) -> None:
    """History whose attachment files were cleaned up still replays: its text is sent and each missing file logged."""
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
    assert sent["content"] == [{"type": text_part, "text": "Compare these."}]
    assert [log["path"] for log in logs if log["event"] == "Model input file not found"] == [str(photo), str(report)]


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

    assert (info.value.code, info.value.retryable) == ("context_length_exceeded", False)


@pytest.mark.asyncio
async def test_an_error_body_without_a_message_reports_the_response_text(tmp_path: Path) -> None:
    """Servers that answer outside the OpenAI error shape still surface what they said."""
    built = _Built(tmp_path, {"provider": "llama_cpp", "id": "qwen"}, _json_reply({"detail": "Not Found"}, status=404))
    request = ModelRequest(messages=[_user()], stream=False, overrides=RequestOverrides(sdk_max_retries=0))

    with pytest.raises(InvalidRequest) as info:
        await _turn(built, request)

    assert (str(info.value), info.value.status_code) == ('{"detail": "Not Found"}', 404)


def test_a_proxy_error_page_becomes_a_retryable_error_with_its_text() -> None:
    """A proxy in front of the endpoint answers 502 with an HTML page, which the SDK keeps as the error body."""
    page = "<html><body>502 Bad Gateway</body></html>"
    response = httpx.Response(502, text=page, request=httpx.Request("POST", "https://proxy.test/v1/chat/completions"))

    error = provider_error(openai.InternalServerError(page, response=response, body=page), provider="OpenAI Chat")

    assert type(error) is ProviderUnavailable
    assert (str(error), error.status_code, error.retryable) == (page, 502, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_config", "reply", "message"),
    [
        ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, _chat_reply(), "OPENROUTER_API_KEY not set"),
        ({"provider": "openai", "api": "responses", "id": "gpt-6-astra"}, _responses_reply(), "OPENAI_API_KEY not set"),
        (
            {
                "provider": "azure",
                "id": "gpt-6-astra",
                "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com"},
            },
            _chat_reply(),
            "Please provide one of: AZURE_OPENAI_API_KEY environment variable, azure_ad_token, or",
        ),
    ],
    ids=["chat", "responses", "azure"],
)
async def test_missing_api_key_fails_at_the_first_request(
    tmp_path: Path,
    model_config: dict[str, Any],
    reply: dict[str, Any],
    message: str,
) -> None:
    built = _Built(tmp_path, model_config, reply)

    with pytest.raises(ProviderError, match=message) as info:
        await _turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, built.requests) == (401, "missing_api_key", [])


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

    with pytest.raises(ProviderError, match=message) as info:
        await _turn(built, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, info.value.retryable) == (401, "authentication", False)
    assert built.requests == []


@pytest.mark.asyncio
async def test_a_codex_login_without_an_account_sends_no_account_header(tmp_path: Path) -> None:
    """A ChatGPT login without an account id still works; the account header is simply left out."""
    codex_home = tmp_path / "login"
    codex_home.mkdir()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800}).encode()).decode().rstrip("=")
    tokens = {"access_token": f"header.{claims}.signature", "refresh_token": "refresh-value"}
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8")
    model_config = {"provider": "codex", "id": "gpt-6.1-sol", "extra_kwargs": {"codex_home": str(codex_home)}}
    built = _Built(tmp_path, model_config, _responses_reply(*_text_events("ok"), _completed()))

    await _turn(built, ModelRequest(messages=[_user()]))

    headers = built.requests[0].headers
    assert headers["authorization"] == f"Bearer {tokens['access_token']}"
    assert "chatgpt-account-id" not in headers


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


_REASONING_ITEM = {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Rain is likely."}]}
_MESSAGE_ITEM = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Pack a coat.", "annotations": []}],
}


@pytest.mark.asyncio
async def test_a_non_streamed_response_keeps_its_reasoning_summary(tmp_path: Path) -> None:
    """A helper call on the Responses route reads the reasoning summary; a stored response needs no reasoning copy."""
    built = _Built(tmp_path, _OPENAI_RESPONSES, _json_reply(_response(output=[_REASONING_ITEM, _MESSAGE_ITEM])))

    turn = await _turn(built, ModelRequest(messages=[_user()], stream=False))

    assert (turn.content, turn.message.reasoning_content) == ("Pack a coat.", "Rain is likely.")
    assert turn.message.provider_data["response_id"] == "resp_1"
    assert "reasoning_output" not in turn.message.provider_data


@pytest.mark.asyncio
async def test_a_non_streamed_failed_response_raises_its_error(tmp_path: Path) -> None:
    failed = _response("failed", error={"code": "server_error", "message": "The server had an error."})
    built = _Built(tmp_path, _OPENAI_RESPONSES, _json_reply(failed))

    with pytest.raises(ProviderUnavailable, match="The server had an error") as info:
        await _adapter_turn(built, ModelRequest(messages=[_user()], stream=False))

    assert (info.value.status_code, info.value.code) == (500, "server_error")


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
        (
            [
                _text_events("")[0],
                _item_event("added", _REASONING_ITEM),
                _item_event("done", _REASONING_ITEM),
                _SERVER_ERROR,
            ],
            False,
            True,
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
        await _turn(built, ModelRequest(messages=[_user()]))

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


_DECISION_TOOL = ToolSpec(name="get_weather", description="Forecast.", parameters={"type": "object"})
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
    """Send ``request`` once as a reply and once as a decision, returning both wire bodies."""
    reply_kind = "responses" if model_config.get("api") == "responses" else "text"
    built = _Built(tmp_path, model_config, _decision_reply(reply_kind))
    await _turn(built, request)
    with without_provider_tools():
        await _turn(built, request)
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
        deferred_tool_names=frozenset({"get_weather"}),
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

    with without_provider_tools(), pytest.raises(ValueError, match=error):
        await _turn(built, ModelRequest(messages=[_user()], tools=[_DECISION_TOOL]))

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
    [("deepseek-flash", {"use_thinking": False}, {"type": "disabled"}), ("deepseek-chat", {}, None)],
    ids=["turned-off", "non-thinking-model"],
)
async def test_deepseek_thinking_follows_use_thinking(
    tmp_path: Path,
    model_id: str,
    use_thinking: dict[str, bool],
    thinking: dict[str, str] | None,
) -> None:
    """Without thinking no reasoning effort is sent; only a config that turns thinking off says so."""
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
    assert "reasoning_effort" not in body


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
    """Cerebras accepts a JSON schema only when every object, the nested models included, is closed."""
    built = _Built(
        tmp_path,
        {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "k"},
        _chat_reply(_chunk({"content": "{}"}, finish_reason="stop")),
    )
    response_format = select_response_format(built.adapter.capabilities, _Extraction).request_format

    await _turn(built, ModelRequest(messages=[_user()], response_format=response_format))

    sent = built.body()["response_format"]["json_schema"]
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


@pytest.mark.asyncio
async def test_responses_tool_parameters_send_the_first_of_several_types(tmp_path: Path) -> None:
    """An MCP tool may declare a nullable parameter as a list of types; Responses receives the first one."""
    tool = ToolSpec(
        name="search_issues",
        description="Search issues.",
        parameters={
            "type": "object",
            "properties": {"label": {"type": ["string", "null"]}, "limit": {"type": "integer"}},
        },
    )
    built = _Built(tmp_path, _OPENAI_RESPONSES, _responses_reply(*_text_events("ok"), _completed()))

    await _turn(built, ModelRequest(messages=[_user()], tools=[tool]))

    sent = built.body()["tools"][0]["parameters"]["properties"]
    assert sent == {"label": {"type": "string"}, "limit": {"type": "integer"}}
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
