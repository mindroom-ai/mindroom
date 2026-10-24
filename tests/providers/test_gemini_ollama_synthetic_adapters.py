"""Behavior of the owned Gemini, Ollama, and synthetic adapters that the C1 goldens do not exercise."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import google.auth.exceptions
import httpx
import pytest
from pydantic import BaseModel

from mindroom.config.models import ModelConfig
from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, TextDelta, ToolSpec, TurnCompleted, UsageReported
from mindroom.providers import remote_media, synthetic_adapter
from mindroom.providers.gemini import adapter as gemini_adapter
from mindroom.providers.gemini import files as gemini_files
from mindroom.providers.gemini import wire as gemini_wire
from mindroom.providers.ollama_adapter import build_provider as build_ollama_provider
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider
from mindroom.providers.tool_policy import without_provider_tools
from tests.providers.gemini_ollama_support import (
    build_adapter,
    gemini_sse,
    hide_provider_environment,
    normalized_request,
    ollama_ndjson,
    run_adapter,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent

GEMINI = {"provider": "google", "id": "gemini-3.1-pro-preview", "api_key": "test-key"}
GEMINI_VERTEX = {
    "provider": "gemini",
    "id": "gemini-3.1-pro-preview",
    "extra_kwargs": {"vertexai": True, "project_id": "test-project", "location": "us-central1"},
}
OLLAMA = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}
WEATHER = ToolSpec(
    name="get_weather",
    description="Return the forecast.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c636001000000050001a5f645400000000049454e44ae426082",
)


@pytest.fixture(autouse=True)
def _isolated_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_provider_environment(monkeypatch)


def _config(base: dict[str, Any], **extra_kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    return {**base, "extra_kwargs": {**base.get("extra_kwargs", {}), **extra_kwargs}}


def _request(*messages: Message, **changes: Any) -> ModelRequest:  # noqa: ANN401
    default_messages = (Message(role="system", content="Be brief."), Message(role="user", content="Hi"))
    return ModelRequest(messages=messages or default_messages, **changes)


def _gemini_chunk(*parts: dict[str, Any], finish: str | None = "STOP", usage: bool = True) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    chunk: dict[str, Any] = {"candidates": [candidate]}
    if usage:
        chunk["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 3, "thoughtsTokenCount": 2}
    return chunk


def _ollama_done(reason: str = "stop", **message: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "model": "qwen3.8:27b",
        "created_at": "2026-10-20T00:00:00Z",
        "done": True,
        "done_reason": reason,
        "message": {"role": "assistant", "content": "", **message},
        "prompt_eval_count": 7,
        "eval_count": 5,
    }


def _completed(events: list[StreamEvent]) -> TurnCompleted:
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_rejects_unknown_extra_kwargs(tmp_path: Path) -> None:
    """A key the Gemini adapter does not take fails at build time with the accepted keys listed."""
    config = ModelConfig(**_config(GEMINI, url_contxt=True))

    with pytest.raises(
        ValueError,
        match=r"Unsupported extra_kwargs for provider 'google': url_contxt\. Accepted keys: ",
    ):
        gemini_adapter.build_provider(config, runtime_paths_for(tmp_path))


def test_gemini_uses_the_shared_google_key_except_on_vertex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without its own key a Gemini model takes the shared Google key; Vertex AI never does."""
    requested: list[str] = []

    def shared_key(provider: str, *, runtime_paths: object) -> str:
        del runtime_paths
        requested.append(provider)
        return "shared-key"

    monkeypatch.setattr(gemini_adapter, "get_api_key_for_provider", shared_key)
    runtime_paths = runtime_paths_for(tmp_path)

    api = gemini_adapter.build_provider(ModelConfig(provider="gemini", id="gemini-3.1-pro-preview"), runtime_paths)
    vertex = gemini_adapter.build_provider(ModelConfig(**GEMINI_VERTEX), runtime_paths)
    own = gemini_adapter.build_provider(ModelConfig(**GEMINI), runtime_paths)

    assert api.settings.api_key == "shared-key"
    assert vertex.settings.api_key is None
    assert vertex.vertexai
    assert own.settings.api_key == "test-key"
    assert requested == ["google"]


@pytest.mark.asyncio
async def test_gemini_reads_what_its_config_leaves_unset_from_the_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOOGLE_API_KEY supplies a missing key, and on Vertex AI the project and location come from their variables."""
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    seen: list[httpx.Request] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return gemini_sse(_gemini_chunk({"text": "ok"}))

    for config in (
        {"provider": "google", "id": "gemini-3.8-flash"},
        {"provider": "gemini", "id": "gemini-3.8-flash", "extra_kwargs": {"vertexai": True}},
    ):
        adapter = build_adapter(config, tmp_path, respond)
        _completed([event async for event in adapter.stream(_request())])

    api, vertex = seen
    assert api.headers["x-goog-api-key"] == "env-key"
    assert "/projects/env-project/locations/europe-west4/" in str(vertex.url)
    assert "x-goog-api-key" not in vertex.headers


def test_gemini_timeout_reaches_the_sdk_client_in_milliseconds(tmp_path: Path) -> None:
    """The ``timeout`` extra kwarg is in seconds, as Agno's field was, and the SDK client takes milliseconds."""
    adapter = gemini_adapter.build_provider(ModelConfig(**_config(GEMINI, timeout=12.5)), runtime_paths_for(tmp_path))

    assert adapter._client_params() == {"api_key": "test-key", "http_options": {"timeout": 12500}}


@pytest.mark.asyncio
async def test_gemini_retries_as_authored_except_for_a_caller_that_retries_itself(tmp_path: Path) -> None:
    """The SDK retries only as ``http_options.retry_options`` authors it, and a summary's request gets one attempt."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503, json={"error": {"code": 503, "message": "Overloaded", "status": "UNAVAILABLE"}})

    http_options = {
        "httpx_async_client": httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        "retry_options": {"attempts": 3, "initial_delay": 0.01, "max_delay": 0.01},
    }
    config = ModelConfig(**_config(GEMINI, client_params={"http_options": http_options}))
    adapter = gemini_adapter.build_provider(config, runtime_paths_for(tmp_path))

    attempts = []
    for overrides in (RequestOverrides(), RequestOverrides(caller_retries=True)):
        sent.clear()
        with pytest.raises(ProviderUnavailable):
            await anext(adapter.stream(_request(stream=False, overrides=overrides)))
        attempts.append(len(sent))

    assert attempts == [3, 1]


@pytest.mark.asyncio
async def test_gemini_settings_and_overrides_shape_the_generation_config(tmp_path: Path) -> None:
    """Authored settings reach the request; overrides replace them, and disabling thinking keeps the authored config.

    Gemini thinks by default, so dropping a ``thinking_budget: 0`` would turn thinking on.
    """
    config = _config(
        GEMINI,
        generation_config={"candidate_count": 1},
        temperature=0.2,
        max_output_tokens=100,
        thinking_budget=0,
        include_thoughts=False,
    )
    response = gemini_sse(_gemini_chunk({"text": "ok"}))

    authored, _ = await run_adapter(config, _request(), tmp_path, response)
    overridden, _ = await run_adapter(
        config,
        _request(overrides=RequestOverrides(temperature=0.7, disable_thinking=True)),
        tmp_path,
        gemini_sse(_gemini_chunk({"text": "ok"})),
    )

    assert authored["body"]["generationConfig"] == {
        "candidateCount": 1,
        "temperature": 0.2,
        "maxOutputTokens": 100,
        "thinkingConfig": {"thinking_budget": 0, "include_thoughts": False},
    }
    assert overridden["body"]["generationConfig"] == {
        "candidateCount": 1,
        "temperature": 0.7,
        "maxOutputTokens": 100,
        "thinkingConfig": {"thinking_budget": 0, "include_thoughts": False},
    }


@pytest.mark.asyncio
async def test_gemini_tool_history_keeps_valid_call_ids_and_drops_invalid_ones(tmp_path: Path) -> None:
    """Gemini matches each result to its call by id, so malformed ids are left out without shifting valid ones."""
    messages = (
        Message(role="user", content="Hi"),
        Message(
            role="assistant",
            tool_calls=[
                {"id": 7, "type": "function", "function": {"name": "invalid", "arguments": "{}"}},
                {"id": "call-123", "type": "function", "function": {"name": "valid", "arguments": "{}"}},
            ],
        ),
        Message(role="tool", tool_call_id="", tool_name="invalid", content="invalid result"),
        Message(role="tool", tool_call_id="call-123", tool_name="valid", content="valid result"),
    )

    sent, _ = await run_adapter(GEMINI, _request(*messages), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    calls, results = (sent["body"]["contents"][index]["parts"] for index in (1, 2))
    assert [part["functionCall"].get("id") for part in calls] == [None, "call-123"]
    assert [part["functionResponse"].get("id") for part in results] == [None, "call-123"]


@pytest.mark.asyncio
async def test_gemini_leaves_out_a_stored_turn_without_text_or_calls(tmp_path: Path) -> None:
    """An assistant turn that kept only its reasoning sends no content, so the user turns around it merge."""
    messages = (
        Message(role="user", content="Hi"),
        Message(role="assistant", reasoning_content="Nothing to add."),
        Message(role="user", content="Still there?"),
    )

    sent, _ = await run_adapter(GEMINI, _request(*messages), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    assert sent["body"]["contents"] == [{"role": "user", "parts": [{"text": "Hi"}, {"text": "Still there?"}]}]


@pytest.mark.asyncio
async def test_gemini_replays_undecodable_tool_arguments_as_empty(tmp_path: Path) -> None:
    """Stored arguments that are not JSON, such as a call cut off mid-stream, replay as empty arguments, as in Agno."""
    messages = (
        Message(role="user", content="Weather?"),
        Message(
            role="assistant",
            tool_calls=[
                {"id": "call-1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Par'}},
            ],
        ),
        Message(role="tool", tool_call_id="call-1", tool_name="get_weather", content="Missing city."),
    )

    sent, _ = await run_adapter(GEMINI, _request(*messages), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    assert sent["body"]["contents"][1]["parts"] == [
        {"functionCall": {"id": "call-1", "name": "get_weather", "args": {}}},
    ]


@pytest.mark.asyncio
async def test_gemini_default_sampling_models_omit_sampling_controls(tmp_path: Path) -> None:
    """Models that require their provider's sampling defaults never receive temperature, top_p, or top_k."""
    config = _config({**GEMINI, "id": "gemini-3.8-flash"}, temperature=0.2, top_p=0.9, top_k=40, seed=7)

    request, _ = await run_adapter(
        config,
        _request(overrides=RequestOverrides(temperature=0.5)),
        tmp_path,
        gemini_sse(_gemini_chunk({"text": "ok"})),
    )

    assert request["body"]["generationConfig"] == {"seed": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize(("config", "json_output"), [(GEMINI, True), (GEMINI_VERTEX, False)])
async def test_gemini_decision_requests_keep_schemas_but_disable_function_calling(
    config: dict[str, Any],
    json_output: bool,
    tmp_path: Path,
) -> None:
    """A participation decision keeps declarations, sets mode NONE, and asks for JSON where Gemini accepts it."""
    with without_provider_tools():
        request, _ = await run_adapter(
            config,
            _request(tools=[WEATHER], tool_choice="none"),
            tmp_path,
            gemini_sse(_gemini_chunk({"text": "{}"})),
        )

    body = request["body"]
    assert [declaration["name"] for declaration in body["tools"][0]["functionDeclarations"]] == ["get_weather"]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
    assert ("responseMimeType" in body["generationConfig"]) is json_output


@pytest.mark.asyncio
async def test_gemini_decision_requests_drop_authored_native_tools(tmp_path: Path) -> None:
    """Grounding, code execution, and URL context run whatever the function-calling mode, so a decision drops them."""
    authored = {
        "tools": [
            {"google_search": {}},
            {"code_execution": {}, "function_declarations": [{"name": "read_status", "description": "Read status"}]},
        ],
    }
    config = _config(GEMINI, generation_config=copy.deepcopy(authored))

    with without_provider_tools():
        request, _ = await run_adapter(config, _request(), tmp_path, gemini_sse(_gemini_chunk({"text": "{}"})))

    body = request["body"]
    assert body["tools"] == [{"functionDeclarations": [{"name": "read_status", "description": "Read status"}]}]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
    assert config["extra_kwargs"]["generation_config"] == authored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_kwargs", "error"),
    [
        ({"cached_content": "cachedContents/example"}, "cached content"),
        ({"generation_config": {"http_options": {"extra_body": {"tools": [{"googleSearch": {}}]}}}}, "body overrides"),
    ],
    ids=["cached-content", "body-override"],
)
async def test_gemini_decision_requests_fail_on_tools_they_cannot_inspect(
    extra_kwargs: dict[str, Any],
    error: str,
    tmp_path: Path,
) -> None:
    """Cached content and raw body overrides can carry native tools, so a decision refuses to send them."""
    with without_provider_tools(), pytest.raises(ValueError, match=error):
        await run_adapter(
            _config(GEMINI, **extra_kwargs),
            _request(),
            tmp_path,
            gemini_sse(_gemini_chunk({"text": "{}"})),
        )


@pytest.mark.asyncio
async def test_gemini_decision_requests_fail_on_client_body_overrides(tmp_path: Path) -> None:
    """Body overrides authored on the SDK client can carry native tools too, so a decision refuses to send them."""
    sent: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(sent.append)) as client:
        http_options = {"extra_body": {"tools": [{"googleSearch": {}}]}, "httpx_async_client": client}
        config = _config(GEMINI, client_params={"http_options": http_options})
        adapter = gemini_adapter.build_provider(ModelConfig(**config), runtime_paths_for(tmp_path))

        with without_provider_tools(), pytest.raises(ValueError, match="body overrides"):
            [event async for event in adapter.stream(_request(tools=[WEATHER], tool_choice="none"))]

    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [GEMINI, GEMINI_VERTEX], ids=["gemini", "vertexai"])
async def test_gemini_decision_requests_with_search_send_no_tools(config: dict[str, Any], tmp_path: Path) -> None:
    """Search replaces the function tools and a decision drops native tools, so nothing is left to call.

    The Agno path sent the same body: no tools, no function-calling config, and JSON output on both routes.
    """
    with without_provider_tools():
        request, _ = await run_adapter(
            _config(config, search=True),
            _request(tools=[WEATHER], tool_choice="none"),
            tmp_path,
            gemini_sse(_gemini_chunk({"text": "{}"})),
        )

    body = request["body"]
    assert "tools" not in body
    assert "toolConfig" not in body
    assert body["generationConfig"] == {"responseMimeType": "application/json"}


def test_gemini_tool_choice_none_turns_function_calling_off() -> None:
    """The wire helper maps a ``"none"`` tool choice to Gemini's function-calling mode NONE.

    No production request reaches it with that choice outside a decision request.
    """
    assert gemini_wire.tool_config("none") == {"function_calling_config": {"mode": "NONE"}}


@pytest.mark.asyncio
async def test_gemini_parses_reasoning_tool_calls_and_usage(tmp_path: Path) -> None:
    """Thoughts become reasoning, a call without an id gets one, and thinking counts as output."""
    response = gemini_sse(
        _gemini_chunk({"text": "Think.", "thought": True}, finish=None, usage=False),
        _gemini_chunk(
            {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}, "thoughtSignature": "c2ln"},
        ),
    )

    _, events = await run_adapter(GEMINI, _request(tools=[WEATHER]), tmp_path, response)

    completed = _completed(events)
    message = completed.message
    assert message.reasoning_content == "Think."
    assert message.content is None
    [call] = message.tool_calls or []
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Paris"}'}
    assert call["thought_signature"] == "c2ln"
    assert call["id"]
    metrics = message.metrics
    assert (
        metrics.tokens.input_tokens,
        metrics.tokens.output_tokens,
        metrics.tokens.total_tokens,
        metrics.tokens.reasoning_tokens,
    ) == (
        10,
        5,
        15,
        2,
    )
    assert completed.stop_reason == "tool_use"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish", "stop"),
    [("MAX_TOKENS", "output_limit"), ("SAFETY", "content_filter"), ("OTHER", "incomplete")],
)
async def test_gemini_stop_reasons(finish: str, stop: str, tmp_path: Path) -> None:
    """Gemini finish reasons map to the normalized stop reasons."""
    _, events = await run_adapter(GEMINI, _request(), tmp_path, gemini_sse(_gemini_chunk({"text": "x"}, finish=finish)))

    assert _completed(events).stop_reason == stop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "response"),
    [
        (GEMINI, gemini_sse(_gemini_chunk({"text": "Hi"}, finish=None, usage=False))),
        (
            OLLAMA,
            ollama_ndjson({"model": "qwen3.8:27b", "done": False, "message": {"role": "assistant", "content": "Hi"}}),
        ),
    ],
    ids=["gemini-without-finish-reason", "ollama-without-done-chunk"],
)
async def test_a_stream_the_provider_never_ended_finishes_no_turn(
    family: dict[str, Any],
    response: httpx.Response,
    tmp_path: Path,
) -> None:
    """A stream cut off before Gemini's finish reason or Ollama's done chunk yields its output but no finished turn.

    The request pipeline turns the missing turn into its retryable early-end error.
    """
    _, events = await run_adapter(family, _request(), tmp_path, response)

    assert events == [TextDelta("Hi")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "reply"),
    [
        (GEMINI, _gemini_chunk({"text": "Hi"}, finish=None, usage=False)),
        (OLLAMA, {"model": "qwen3.8:27b", "done": False, "message": {"role": "assistant", "content": "Hi"}}),
    ],
    ids=["gemini", "ollama"],
)
async def test_a_whole_reply_finishes_its_turn_without_the_marker_a_stream_ends_with(
    family: dict[str, Any],
    reply: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Only a stream can be cut off, so a reply that arrives in one response body always finishes its turn."""
    _, events = await run_adapter(family, _request(stream=False), tmp_path, httpx.Response(200, json=reply))

    assert _completed(events).message.content == "Hi"


@pytest.mark.asyncio
async def test_gemini_malformed_function_call_fails_with_its_code(tmp_path: Path) -> None:
    """A malformed function call ends the stream with an error the loop can answer with guidance."""
    response = gemini_sse(_gemini_chunk(finish="MALFORMED_FUNCTION_CALL"))

    with pytest.raises(ProviderError) as raised:
        await run_adapter(GEMINI, _request(), tmp_path, response)

    assert raised.value.code == MALFORMED_TOOL_CALL
    assert not raised.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (429, "Resource has been exhausted", RateLimited),
        (503, "The model is overloaded", ProviderUnavailable),
        (
            400,
            "The input token count (2000000) exceeds the maximum number of tokens allowed (1048576).",
            ContextWindowExceeded,
        ),
        (400, "Invalid JSON payload received.", InvalidRequest),
        (403, "Permission denied", ProviderError),
    ],
)
async def test_gemini_http_errors_are_normalized(
    status: int,
    message: str,
    error_type: type[ProviderError],
    tmp_path: Path,
) -> None:
    """SDK API errors become the normalized error for their status and wording."""
    response = httpx.Response(status, json={"error": {"code": status, "message": message, "status": "FAILED"}})

    with pytest.raises(ProviderError) as raised:
        await run_adapter(GEMINI, _request(), tmp_path, response)

    assert type(raised.value) is error_type
    assert raised.value.status_code == status
    assert raised.value.provider == "Google"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed_call", "missing_file"])
async def test_gemini_normalized_failures_do_not_cause_themselves(failure: str, tmp_path: Path) -> None:
    """An error that arrives normalized is re-raised as it is.

    A ``__cause__`` pointing at the error itself makes Rich's traceback renderer, and so the colored console log,
    loop forever.
    """
    missing = Message(role="user", content="Look", images=[Image(filepath=str(tmp_path / "missing.png"))])
    request = _request() if failure == "malformed_call" else _request(missing)
    response = gemini_sse(_gemini_chunk(finish="MALFORMED_FUNCTION_CALL"))

    with pytest.raises(ProviderError) as raised:
        await run_adapter(GEMINI, request, tmp_path, response)

    assert raised.value.__cause__ is not raised.value
    assert raised.value.__context__ is not raised.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("cut"),
        TimeoutError(),
        ConnectionResetError(104, "Connection reset by peer"),
        google.auth.exceptions.TransportError("token endpoint unreachable"),
    ],
    ids=[
        "httpx-connect",
        "aiohttp-disconnected",
        "aiohttp-payload",
        "aiohttp-total-timeout",
        "connection-reset",
        "vertex-token-refresh",
    ],
)
async def test_gemini_transport_failure_is_retryable(failure: Exception, tmp_path: Path) -> None:
    """A connection failure before any reply is retryable, on the httpx transport and on aiohttp.

    Production uses aiohttp, which lets its total timeout and a reset connection escape as plain ``OSError``s.
    """
    with pytest.raises(ProviderUnavailable) as raised:
        await run_adapter(GEMINI, _request(), tmp_path, failure)

    assert raised.value.retryable
    assert raised.value.__cause__ is failure
    assert str(raised.value)


@pytest.mark.asyncio
async def test_gemini_rejects_an_authored_setting_the_sdk_cannot_validate(tmp_path: Path) -> None:
    """A generation setting the SDK's types reject fails as an invalid request instead of a raw validation error."""
    config = _config(GEMINI, generation_config={"candidate_count": "many"})

    with pytest.raises(InvalidRequest) as raised:
        await run_adapter(config, _request(), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    assert not raised.value.retryable
    assert "candidate_count" in str(raised.value)


@pytest.mark.asyncio
async def test_gemini_credential_failures_are_not_provider_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials that cannot be loaded are a configuration error, not a retryable provider failure."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing-credentials.json"))
    config = {**GEMINI_VERTEX, "extra_kwargs": dict(GEMINI_VERTEX["extra_kwargs"])}
    adapter = gemini_adapter.build_provider(ModelConfig(**config), runtime_paths_for(tmp_path))

    with pytest.raises(Exception, match=r"missing-credentials\.json") as raised:
        [event async for event in adapter.stream(_request())]

    assert not isinstance(raised.value, ProviderError)


@pytest.mark.asyncio
async def test_gemini_usage_counts_thinking_as_output_and_keeps_cached_input(tmp_path: Path) -> None:
    """Google bills thinking as output, and cached input stays inside the prompt count."""
    chunk = _gemini_chunk({"text": "ok"}, usage=False)
    chunk["usageMetadata"] = {
        "promptTokenCount": 1000,
        "cachedContentTokenCount": 400,
        "candidatesTokenCount": 200,
        "thoughtsTokenCount": 1500,
        "totalTokenCount": 2700,
    }

    _, events = await run_adapter(GEMINI, _request(), tmp_path, gemini_sse(chunk))

    metrics = _completed(events).message.metrics
    assert (metrics.tokens.input_tokens, metrics.tokens.output_tokens, metrics.tokens.total_tokens) == (
        1000,
        1700,
        2700,
    )
    assert (metrics.tokens.reasoning_tokens, metrics.tokens.cache_read_tokens) == (1500, 400)


@pytest.mark.asyncio
async def test_gemini_non_streaming_requests_use_generate_content(tmp_path: Path) -> None:
    """``stream=False`` sends the route's non-streaming call and yields only the usage and the finished turn."""
    reply = httpx.Response(200, json=_gemini_chunk({"text": "Whole."}))

    request, events = await run_adapter(GEMINI, _request(stream=False), tmp_path, reply)

    assert request["url"].endswith(":generateContent")
    completed = _completed(events)
    assert [type(event).__name__ for event in events] == ["UsageReported", "UsageReported", "TurnCompleted"]
    assert completed.message.content == "Whole."
    assert completed.message.metrics.tokens.input_tokens == 10


@pytest.mark.asyncio
async def test_gemini_chunks_without_output_are_reported_as_progress(tmp_path: Path) -> None:
    """A chunk that only shows a native tool at work keeps the idle limit alive without becoming output."""
    response = gemini_sse(
        _gemini_chunk({"executableCode": {"language": "PYTHON", "code": "print(1)"}}, finish=None, usage=False),
        _gemini_chunk({"text": "2"}),
    )

    _, events = await run_adapter(GEMINI, _request(), tmp_path, response)

    assert [type(event).__name__ for event in events] == [
        "StreamProgress",
        "UsageReported",
        "TextDelta",
        "UsageReported",
        "TurnCompleted",
    ]


# Gemini answers a prompt it blocks with feedback and usage but no candidate.
_GEMINI_BLOCKED_PROMPT = {
    "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
    "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9},
}


@pytest.mark.asyncio
async def test_gemini_blocked_prompt_ends_the_turn_without_output(tmp_path: Path) -> None:
    """A streamed prompt Gemini blocks ends the turn empty and, as in Agno's streamed parser, reports no usage."""
    _, events = await run_adapter(GEMINI, _request(), tmp_path, gemini_sse(_GEMINI_BLOCKED_PROMPT))

    completed = _completed(events)
    assert [type(event).__name__ for event in events] == ["StreamProgress", "TurnCompleted"]
    assert (completed.message.content, completed.message.tool_calls, completed.stop_reason) == (None, None, None)


@pytest.mark.asyncio
async def test_gemini_non_streamed_blocked_prompt_reports_its_prompt_usage(tmp_path: Path) -> None:
    """A non-streamed blocked prompt, such as a participation decision, still reports its billed prompt tokens."""
    reply = httpx.Response(200, json=_GEMINI_BLOCKED_PROMPT)

    _, events = await run_adapter(GEMINI, _request(stream=False), tmp_path, reply)

    completed = _completed(events)
    assert [type(event).__name__ for event in events] == ["UsageReported", "UsageReported", "TurnCompleted"]
    usage = [event.usage for event in events if isinstance(event, UsageReported) and event.final]
    assert [
        (metrics.tokens.input_tokens, metrics.tokens.output_tokens, metrics.tokens.total_tokens) for metrics in usage
    ] == [(9, 0, 9)]
    assert completed.message.metrics.tokens.input_tokens == 9
    assert (completed.message.content, completed.message.tool_calls, completed.stop_reason) == (None, None, None)


@pytest.mark.asyncio
async def test_gemini_empty_text_parts_add_no_deltas_but_keep_their_signature(tmp_path: Path) -> None:
    """Gemini can stream an empty thought and close with an empty text part that carries the thought signature."""
    response = gemini_sse(
        _gemini_chunk({"text": "", "thought": True}, finish=None, usage=False),
        _gemini_chunk({"text": "Hi"}, finish=None, usage=False),
        _gemini_chunk({"text": "", "thoughtSignature": "c2ln"}),
    )

    _, events = await run_adapter(GEMINI, _request(), tmp_path, response)

    message = _completed(events).message
    assert [type(event).__name__ for event in events] == [
        "StreamProgress",
        "TextDelta",
        "UsageReported",
        "UsageReported",
        "TurnCompleted",
    ]
    assert (message.content, message.reasoning_content) == ("Hi", None)
    assert message.provider_data is not None
    assert message.provider_data["thought_signature"] == "c2ln"


@pytest.mark.asyncio
async def test_gemini_upload_of_a_vanished_file_is_an_invalid_request(tmp_path: Path) -> None:
    """A local file that disappears before its upload fails the request without retries."""
    from google import genai  # noqa: PLC0415 - the SDK client is only built for this probe

    client = genai.Client(api_key="test-key")

    with pytest.raises(InvalidRequest, match="could not be read"):
        await gemini_files.uploaded_file_uri(client, tmp_path / "gone.wav", "audio/wav", provider="Google")


_GEMINI_API = "https://generativelanguage.googleapis.com/"
_UPLOAD_SESSION = "https://upload.test/session"
_STREAM_SUFFIX = ":streamGenerateContent?alt=sse"


@dataclass
class _MediaServer:
    """Answers model requests, the Gemini Files API, and remote media URLs, recording every request."""

    remote: dict[str, httpx.Response] = field(default_factory=dict)
    # How many Files API checks still report PROCESSING after an upload, and the state that follows.
    processing_checks: int = 1
    processed_state: str = "ACTIVE"
    # Another request uploads the same content between this request's check and its upload.
    uploaded_concurrently: bool = False
    # Raised by the transport while the upload sends the file's bytes.
    upload_failure: Exception | None = None
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    _pending: dict[str, Any] = field(default_factory=dict)
    _checks_left: dict[str, int] = field(default_factory=dict)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.remote:
            return self.remote[url]
        if url.endswith(_STREAM_SUFFIX):
            return gemini_sse(_gemini_chunk({"text": "ok"}))
        if url.endswith("/api/chat"):
            return ollama_ndjson(_ollama_done())
        if url == f"{_GEMINI_API}upload/v1beta/files":
            return self._start_upload(json.loads(request.content)["file"])
        if url == _UPLOAD_SESSION:
            if self.upload_failure is not None:
                raise self.upload_failure
            return self._finish_upload()
        if request.method == "GET" and url.startswith(f"{_GEMINI_API}v1beta/files/"):
            return self._file(f"files/{url.rsplit('/', 1)[1]}")
        msg = f"unexpected request {request.method} {url}"
        raise AssertionError(msg)

    def _start_upload(self, file: dict[str, Any]) -> httpx.Response:
        self._pending = file
        if self.uploaded_concurrently:
            name = file["name"]
            self.files[name] = {"name": name, "uri": f"{_GEMINI_API}v1beta/{name}", "state": "ACTIVE"}
            error = {"code": 409, "message": "File already exists", "status": "ALREADY_EXISTS"}
            return httpx.Response(409, json={"error": error})
        return httpx.Response(200, headers={"x-goog-upload-url": _UPLOAD_SESSION}, json={})

    def _finish_upload(self) -> httpx.Response:
        name = self._pending["name"]
        self.uploads.append(name)
        self.files[name] = {
            "name": name,
            "mimeType": self._pending["mime_type"],
            "uri": f"{_GEMINI_API}v1beta/{name}",
            "state": "PROCESSING",
        }
        self._checks_left[name] = self.processing_checks
        return httpx.Response(200, headers={"x-goog-upload-status": "final"}, json={"file": self.files[name]})

    def _file(self, name: str) -> httpx.Response:
        file = self.files.get(name)
        if file is None:
            # The Gemini API answers 403 for a file name it does not know.
            error = {"code": 403, "message": "You do not have permission to access the File", "status": "DENIED"}
            return httpx.Response(403, json={"error": error})
        if file["state"] == "PROCESSING":
            if self._checks_left[name]:
                self._checks_left[name] -= 1
            else:
                file["state"] = self.processed_state
        return httpx.Response(200, json=file)

    def model_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if str(request.url).endswith((_STREAM_SUFFIX, "/api/chat"))]


def _upload_name(mime_type: str, data: bytes) -> str:
    return f"files/{hashlib.sha256(f'{mime_type}\0'.encode() + data).hexdigest()[:40]}"


def _uploaded(name: str, mime_type: str) -> dict[str, Any]:
    return {"fileData": {"fileUri": f"{_GEMINI_API}v1beta/{name}", "mimeType": mime_type}}


async def _stream_media(
    model_config: dict[str, Any],
    request: ModelRequest,
    tmp_path: Path,
    server: _MediaServer,
    monkeypatch: pytest.MonkeyPatch,
) -> list[StreamEvent]:
    """Stream ``request`` with model, Files API, and remote media traffic answered by ``server``."""
    adapter = build_adapter(model_config, tmp_path, server)
    transport = httpx.MockTransport(server)
    monkeypatch.setattr(remote_media, "_http_client", lambda: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(gemini_files, "_PROCESSING_POLL_SECONDS", 0)
    return [event async for event in adapter.stream(request)]


def _user_parts(server: _MediaServer) -> list[dict[str, Any]]:
    return normalized_request(server.model_requests()[-1])["body"]["contents"][0]["parts"]


@pytest.mark.asyncio
async def test_gemini_inlines_local_media_and_uploads_what_is_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local media up to the inline limit is inlined; a larger file goes through the Files API."""
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(PNG)
    large = b"%PDF" + b"0" * 256
    large_path = tmp_path / "large.pdf"
    large_path.write_bytes(large)
    monkeypatch.setattr(gemini_wire, "_INLINE_MEDIA_LIMIT_BYTES", 128)
    message = Message(
        role="user",
        content="Look",
        images=[Image(filepath=str(image_path))],
        files=[File(filepath=str(large_path)), File(url="gs://bucket/notes.pdf")],
    )
    server = _MediaServer()

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    name = _upload_name("application/pdf", large)
    assert server.uploads == [name]
    assert _user_parts(server) == [
        {"text": "Look"},
        {"inlineData": {"data": base64.urlsafe_b64encode(PNG).decode(), "mimeType": "image/png"}},
        _uploaded(name, "application/pdf"),
        {"fileData": {"fileUri": "gs://bucket/notes.pdf", "mimeType": "application/pdf"}},
    ]


@pytest.mark.asyncio
async def test_gemini_uploads_local_audio_and_video_once_and_reuses_the_active_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local audio and video go through the Files API whatever their size, and the same content is uploaded once."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4-clip")
    message = Message(
        role="user",
        content="Summarize",
        audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")],
        videos=[Video(filepath=str(video_path))],
    )
    server = _MediaServer(processing_checks=2)

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)
    first_parts = _user_parts(server)
    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    video_name = _upload_name("video/mp4", b"mp4-clip")
    audio_name = _upload_name("audio/ogg", b"OggS-voice")
    assert server.uploads == [video_name, audio_name]
    assert first_parts == [
        _uploaded(video_name, "video/mp4"),
        {"text": "Summarize"},
        _uploaded(audio_name, "audio/ogg"),
    ]
    assert _user_parts(server) == first_parts


@pytest.mark.asyncio
async def test_gemini_uses_the_upload_a_concurrent_request_made_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team members sending the same attachment at once share one upload instead of failing on the name clash."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    message = Message(role="user", content="Listen", audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")])
    server = _MediaServer(uploaded_concurrently=True)

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert server.uploads == []
    assert _user_parts(server)[1] == _uploaded(_upload_name("audio/ogg", b"OggS-voice"), "audio/ogg")


@pytest.mark.asyncio
async def test_gemini_upload_the_files_api_cannot_process_is_an_invalid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed upload raises before the model request, so the media fallback can note the missing media."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    message = Message(role="user", content="Listen", audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")])
    server = _MediaServer(processed_state="FAILED")

    with pytest.raises(InvalidRequest) as raised:
        await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert not raised.value.retryable
    assert server.model_requests() == []


@pytest.mark.asyncio
async def test_gemini_file_that_vanishes_before_its_upload_is_an_invalid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file deleted after its upload name was computed is unsendable media, which is never retried."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    content_name = gemini_files._content_name

    def name_then_vanish(path: Path, mime_type: str) -> str:
        name = content_name(path, mime_type)
        path.unlink()
        return name

    monkeypatch.setattr(gemini_files, "_content_name", name_then_vanish)
    message = Message(role="user", content="Listen", audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")])
    server = _MediaServer()

    with pytest.raises(InvalidRequest, match="did not complete") as raised:
        await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert isinstance(raised.value.__cause__, FileNotFoundError)
    assert server.model_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), aiohttp.ClientOSError(104, "Connection reset by peer"), ConnectionResetError(104, "reset")],
    ids=["aiohttp-total-timeout", "aiohttp-os-error", "connection-reset"],
)
async def test_gemini_upload_transport_failure_is_retryable(
    failure: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout or reset while uploading is the provider's transient failure, not unsendable media."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    message = Message(role="user", content="Listen", audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")])
    server = _MediaServer(upload_failure=failure)

    with pytest.raises(ProviderUnavailable) as raised:
        await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert raised.value.retryable
    assert raised.value.__cause__ is failure
    assert server.model_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "status", "state", "error_type"),
    [("lookup", 500, "INTERNAL", ProviderUnavailable), ("upload", 429, "RESOURCE_EXHAUSTED", RateLimited)],
)
async def test_gemini_files_api_failures_are_provider_errors(
    failing: str,
    status: int,
    state: str,
    error_type: type[ProviderError],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Files API failure other than a missing file or a name clash fails the request as the provider's error."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    message = Message(role="user", content="Listen", audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")])
    files_api = {
        "lookup": ("GET", f"{_GEMINI_API}v1beta/{_upload_name('audio/ogg', b'OggS-voice')}"),
        "upload": ("POST", f"{_GEMINI_API}upload/v1beta/files"),
    }
    error = {"code": status, "message": "The Files API request failed.", "status": state}
    # The server answers a URL in ``remote`` before routing it, so this Files API call fails.
    server = _MediaServer(remote={files_api[failing][1]: httpx.Response(status, json={"error": error})})

    with pytest.raises(error_type) as raised:
        await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert raised.value.retryable
    assert raised.value.status_code == status
    # The request stops at the failing call: a failed lookup starts no upload, and no model request follows either.
    expected = {"lookup": [files_api["lookup"]], "upload": [files_api["lookup"], files_api["upload"]]}[failing]
    assert [(request.method, str(request.url)) for request in server.requests] == expected


@pytest.mark.asyncio
async def test_gemini_on_vertex_inlines_local_media_and_rejects_what_is_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex AI has no Files API: local audio is inlined, and media above the inline limit cannot be sent."""
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-voice")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4" + b"0" * 256)
    monkeypatch.setattr(gemini_wire, "_INLINE_MEDIA_LIMIT_BYTES", 128)
    audio = Audio(filepath=str(audio_path), mime_type="audio/ogg")
    server = _MediaServer()

    await _stream_media(
        GEMINI_VERTEX,
        _request(Message(role="user", content="Hi", audio=[audio])),
        tmp_path,
        server,
        monkeypatch,
    )
    with pytest.raises(InvalidRequest):
        await _stream_media(
            GEMINI_VERTEX,
            _request(Message(role="user", content="Hi", videos=[Video(filepath=str(video_path))])),
            tmp_path,
            server,
            monkeypatch,
        )

    assert [str(request.url).endswith(_STREAM_SUFFIX) for request in server.requests] == [True]
    assert _user_parts(server) == [
        {"text": "Hi"},
        # The Vertex AI body names the MIME type in snake case, as the goldens record.
        {"inlineData": {"data": base64.urlsafe_b64encode(b"OggS-voice").decode(), "mime_type": "audio/ogg"}},
    ]


@pytest.mark.asyncio
async def test_gemini_downloads_remote_images_audio_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remote images, audio, and files Gemini cannot fetch are downloaded and inlined; remote video is referenced."""
    server = _MediaServer(
        remote={
            "https://media.test/generated": httpx.Response(200, content=PNG),
            "https://media.test/speech": httpx.Response(200, content=b"RIFF-speech"),
            "http://media.test/notes": httpx.Response(200, headers={"content-type": "text/plain"}, content=b"notes"),
        },
    )
    message = Message(
        role="user",
        content="Use these",
        images=[Image(url="https://media.test/generated")],
        audio=[Audio(url="https://media.test/speech", format="wav")],
        files=[File(url="http://media.test/notes")],
        videos=[Video(url="https://media.test/clip.mp4")],
    )

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert _user_parts(server) == [
        {"fileData": {"fileUri": "https://media.test/clip.mp4", "mimeType": "video/mp4"}},
        {"text": "Use these"},
        {"inlineData": {"data": base64.urlsafe_b64encode(PNG).decode(), "mimeType": "image/png"}},
        {"inlineData": {"data": base64.urlsafe_b64encode(b"RIFF-speech").decode(), "mimeType": "audio/wav"}},
        {"inlineData": {"data": base64.urlsafe_b64encode(b"notes").decode(), "mimeType": "text/plain"}},
    ]


@pytest.mark.asyncio
async def test_gemini_references_an_https_file_of_known_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPS file with a declared type, such as a tool returns, goes out as a URI that Gemini fetches itself."""
    url = "https://media.test/report.pdf"
    message = Message(role="user", content="Read this", files=[File(url=url, mime_type="application/pdf")])
    server = _MediaServer()

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert server.requests == server.model_requests()
    assert _user_parts(server) == [
        {"text": "Read this"},
        {"fileData": {"fileUri": url, "mimeType": "application/pdf"}},
    ]


@pytest.mark.asyncio
async def test_gemini_leaves_out_empty_local_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero-byte local image or audio file adds no part and is never uploaded."""
    image_path = tmp_path / "blank.png"
    image_path.write_bytes(b"")
    audio_path = tmp_path / "silence.ogg"
    audio_path.write_bytes(b"")
    message = Message(
        role="user",
        content="Look",
        images=[Image(filepath=str(image_path))],
        audio=[Audio(filepath=str(audio_path), mime_type="audio/ogg")],
    )
    server = _MediaServer()

    await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    assert server.requests == server.model_requests()
    assert _user_parts(server) == [{"text": "Look"}]


@pytest.mark.asyncio
async def test_gemini_local_audio_of_unknown_type_is_sent_as_mp3(tmp_path: Path) -> None:
    """Audio without a declared type whose suffix names no known format gets Agno's default type."""
    audio_path = tmp_path / "memo.m4a"
    audio_path.write_bytes(b"m4a-memo")
    message = Message(role="user", content="Transcribe", audio=[Audio(filepath=str(audio_path))])

    request, _ = await run_adapter(
        GEMINI_VERTEX,
        _request(message),
        tmp_path,
        gemini_sse(_gemini_chunk({"text": "ok"})),
    )

    assert request["body"]["contents"][0]["parts"][1]["inlineData"]["mime_type"] == "audio/mp3"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing_file", "unreachable_url", "oversized_url"])
async def test_gemini_media_it_cannot_send_is_an_invalid_request(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Media that cannot be read or downloaded raises before the model request instead of being dropped silently."""
    monkeypatch.setattr(gemini_wire, "_INLINE_MEDIA_LIMIT_BYTES", 128)
    images = {
        "missing_file": Image(filepath=str(tmp_path / "missing.png")),
        "unreachable_url": Image(url="https://media.test/gone"),
        "oversized_url": Image(url="https://media.test/huge"),
    }
    server = _MediaServer(
        remote={
            "https://media.test/gone": httpx.Response(404),
            "https://media.test/huge": httpx.Response(200, content=b"0" * 256),
        },
    )
    message = Message(role="user", content="Look", images=[images[source]])

    with pytest.raises(InvalidRequest) as raised:
        await _stream_media(GEMINI, _request(message), tmp_path, server, monkeypatch)

    # The media fallback strips media and retries only for a non-retryable error with no exempt code.
    assert not raised.value.retryable
    assert raised.value.code is None
    assert server.model_requests() == []


@pytest.mark.asyncio
async def test_gemini_places_videos_before_the_prompt_and_other_media_after(tmp_path: Path) -> None:
    """Videos lead the user turn, last one first; images, audio, and files follow the text in that order."""
    message = Message(
        role="user",
        content="Compare",
        files=[File(content=b"%PDF", filename="a.pdf")],
        audio=[Audio(content=b"RIFF", mime_type="audio/wav")],
        images=[Image(content=PNG)],
        videos=[Video(url="gs://bucket/first.mp4"), Video(content=b"mp4", format="webm")],
    )

    request, _ = await run_adapter(GEMINI, _request(message), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    parts = request["body"]["contents"][0]["parts"]
    assert [next(iter(part)) for part in parts] == [
        "inlineData",
        "fileData",
        "text",
        "inlineData",
        "inlineData",
        "inlineData",
    ]
    assert [part["inlineData"]["mimeType"] for part in parts if "inlineData" in part] == [
        "video/webm",
        "image/png",
        "audio/wav",
        "application/pdf",
    ]


def test_gemini_schema_conversion_breaks_cycles_and_leaves_the_input_alone() -> None:
    """Self references end in a placeholder object, and nullable type lists do not rewrite the caller's schema."""
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}, "note": {"type": ["string", "null"]}},
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    original = copy.deepcopy(schema)

    converted = gemini_wire.convert_schema(schema)

    assert converted == {
        "type": "OBJECT",
        "required": [],
        "properties": {
            "child": {
                "type": "OBJECT",
                "required": [],
                "properties": {"child": {"type": "OBJECT", "description": "Circular reference to #/$defs/Node"}},
            },
            "note": {"type": "STRING", "nullable": True},
        },
    }
    assert schema == original


def test_gemini_converts_mcp_tool_schemas_as_agno_did() -> None:
    """Unions, type lists, dict values, empty objects, and unresolved or untyped properties convert as Agno's did.

    MCP and plugin tools send their own JSON schemas, so any of these can reach a Gemini request.
    """
    schema = {
        "type": "object",
        "properties": {
            "issue": {"anyOf": [{"type": "string"}, {"type": "integer"}], "description": "Issue key or number."},
            "limit": {"type": ["integer", "string"]},
            "labels": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
            "weights": {"type": "object", "additionalProperties": {"type": ["number", "string"]}},
            "filter": {"type": "object", "properties": {}, "description": "Field filters."},
            "parent": {"$ref": "#/$defs/Issue", "description": "Parent issue."},
            "payload": {"description": "Any JSON value."},
        },
        "required": ["issue"],
    }

    assert gemini_wire.convert_schema(schema) == {
        "type": "OBJECT",
        "required": ["issue"],
        "properties": {
            "issue": {"any_of": [{"type": "STRING"}, {"type": "INTEGER"}], "description": "Issue key or number."},
            "limit": {"type": "INTEGER"},
            "labels": {
                "type": "OBJECT",
                "properties": {
                    "example_key": {
                        "type": "ARRAY",
                        "description": "Example key-value pair. This object can contain any number of keys with array values.",
                        "items": {},
                    },
                },
                "description": "Dictionary with array values. Can contain any number of key-value pairs.",
            },
            "weights": {
                "type": "OBJECT",
                "properties": {
                    "example_key": {
                        "type": "NUMBER",
                        "description": (
                            "Example key-value pair. This object can contain any number of keys with number values"
                            " (supports union types: number, string)."
                        ),
                    },
                },
                "description": (
                    "Dictionary with number values (supports union types: number, string)."
                    " Can contain any number of key-value pairs."
                ),
            },
            "filter": {"type": "OBJECT", "description": "Field filters."},
            "parent": {"description": "Parent issue."},
            "payload": {"description": "Any JSON value."},
        },
    }


class Extraction(BaseModel):
    """An output model with a dict field."""

    aliases: dict[str, list[str]]


class Topic(BaseModel):
    """An output model that refers to itself."""

    name: str
    subtopics: list[Topic] = []


@pytest.mark.parametrize(
    ("output_model", "expected"),
    [
        (
            Extraction,
            {
                "type": "OBJECT",
                "title": "Extraction",
                "description": "An output model with a dict field.",
                "required": ["aliases"],
                "properties": {
                    "aliases": {
                        "type": "OBJECT",
                        "properties": {
                            "example_key": {
                                "type": "ARRAY",
                                "description": (
                                    "Example key-value pair. This object can contain any number of keys with array"
                                    " values."
                                ),
                                "items": {},
                            },
                        },
                        "description": "Dictionary with array values. Can contain any number of key-value pairs.",
                    },
                },
            },
        ),
        (
            Topic,
            {
                "type": "OBJECT",
                "title": "Topic",
                "description": "An output model that refers to itself.",
                "required": ["name"],
                "properties": {
                    "name": {"type": "STRING", "title": "Name"},
                    "subtopics": {
                        "type": "ARRAY",
                        "title": "Subtopics",
                        "items": {"type": "OBJECT", "description": "Circular reference to #/$defs/Topic"},
                    },
                },
            },
        ),
    ],
    ids=["dict-field", "self-reference"],
)
def test_gemini_converts_output_models_it_cannot_take_as_a_class(
    output_model: type[BaseModel],
    expected: dict[str, Any],
) -> None:
    """Gemini cannot take a model with dict fields or self references as a class, so its schema is converted."""
    assert gemini_wire.response_schema(output_model) == expected


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def test_ollama_rejects_unknown_extra_kwargs(tmp_path: Path) -> None:
    """Keys the Ollama adapter does not take fail at build time."""
    config = ModelConfig(**_config(OLLAMA, temperature=0.3))

    with pytest.raises(ValueError, match="Unsupported extra_kwargs for provider 'ollama': temperature"):
        build_ollama_provider(config, runtime_paths_for(tmp_path))


def test_ollama_host_and_key_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host falls back to the local default, and OLLAMA_API_KEY supplies a missing key."""
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")
    provider = build_ollama_provider(ModelConfig(provider="ollama", id="qwen3.8:27b"), runtime_paths_for(tmp_path))

    assert provider.identity.endpoint == "http://localhost:11434"
    assert provider.settings.api_key == "env-key"


@pytest.mark.asyncio
async def test_ollama_native_settings_reach_the_chat_request(tmp_path: Path) -> None:
    """Options, keep_alive, format, and request_params pass through; a temperature override changes no option.

    Agno's Ollama model had no temperature field, so its options were always the authored ones.
    """
    config = _config(
        OLLAMA,
        options={"num_ctx": 8192},
        keep_alive="5m",
        format="json",
        request_params={"think": False},
    )

    request, _ = await run_adapter(
        config,
        _request(overrides=RequestOverrides(temperature=0.1)),
        tmp_path,
        ollama_ndjson(_ollama_done()),
    )

    body = request["body"]
    assert body["options"] == {"num_ctx": 8192}
    assert body["keep_alive"] == "5m"
    assert body["format"] == "json"
    assert body["think"] is False
    assert request["url"] == "http://ollama.test:11434/api/chat"


@pytest.mark.asyncio
async def test_ollama_api_key_and_timeout_reach_the_http_client(tmp_path: Path) -> None:
    """An API key authenticates against Ollama's hosted endpoint, and ``timeout`` bounds every HTTP phase."""
    seen: list[httpx.Request] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return ollama_ndjson(_ollama_done())

    config = ModelConfig(
        **_config(
            OLLAMA,
            api_key="ollama-key",
            timeout=12.5,
            client_params={"transport": httpx.MockTransport(respond)},
        ),
    )
    provider = build_ollama_provider(config, runtime_paths_for(tmp_path))

    _completed([event async for event in provider.stream(_request())])

    assert seen[0].headers["authorization"] == "Bearer ollama-key"
    assert seen[0].extensions["timeout"] == {"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5}


@pytest.mark.asyncio
async def test_ollama_tools_are_sorted_and_leave_out_empty_descriptions(tmp_path: Path) -> None:
    """Tools go out sorted by name, and a tool without a description sends none, as Agno's did."""
    quiet = ToolSpec(name="a_tool", description=None, parameters={"type": "object", "properties": {}})

    request, _ = await run_adapter(OLLAMA, _request(tools=[WEATHER, quiet]), tmp_path, ollama_ndjson(_ollama_done()))

    assert [tool["function"] for tool in request["body"]["tools"]] == [
        {"name": "a_tool", "parameters": {"type": "object", "properties": {}}},
        {"name": "get_weather", "description": "Return the forecast.", "parameters": dict(WEATHER.parameters)},
    ]


@pytest.mark.asyncio
async def test_ollama_decision_requests_send_no_tools(tmp_path: Path) -> None:
    """Ollama has no tool_choice, so a participation decision leaves the tools out."""
    with without_provider_tools():
        request, _ = await run_adapter(
            OLLAMA,
            _request(tools=[WEATHER], tool_choice="none"),
            tmp_path,
            ollama_ndjson(_ollama_done()),
        )

    assert request["body"]["tools"] == []


@pytest.mark.asyncio
async def test_ollama_reads_local_images(tmp_path: Path) -> None:
    """Local image attachments are read off the event loop and sent as base64."""
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(PNG)

    request, _ = await run_adapter(
        OLLAMA,
        _request(Message(role="user", content="Look", images=[Image(filepath=str(image_path))])),
        tmp_path,
        ollama_ndjson(_ollama_done()),
    )

    assert request["body"]["messages"][0]["images"] == [base64.b64encode(PNG).decode()]


@pytest.mark.asyncio
async def test_ollama_downloads_remote_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Images a tool returned as URLs are downloaded and sent as base64."""
    server = _MediaServer(remote={"https://media.test/generated": httpx.Response(200, content=PNG)})
    message = Message(role="user", content="Look", images=[Image(url="https://media.test/generated")])

    await _stream_media(OLLAMA, _request(message), tmp_path, server, monkeypatch)

    body = normalized_request(server.model_requests()[-1])["body"]
    assert body["messages"][-1]["images"] == [base64.b64encode(PNG).decode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing_file", "unreachable_url"])
async def test_ollama_images_it_cannot_read_are_an_invalid_request(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image that cannot be read or downloaded raises before the chat request instead of being dropped."""
    images = {
        "missing_file": Image(filepath=str(tmp_path / "missing.png")),
        "unreachable_url": Image(url="https://media.test/gone"),
    }
    server = _MediaServer(remote={"https://media.test/gone": httpx.Response(404)})

    with pytest.raises(InvalidRequest) as raised:
        await _stream_media(
            OLLAMA,
            _request(Message(role="user", content="Look", images=[images[source]])),
            tmp_path,
            server,
            monkeypatch,
        )

    assert not raised.value.retryable
    assert server.model_requests() == []


@pytest.mark.asyncio
async def test_ollama_parses_text_tool_calls_usage_and_length_stops(tmp_path: Path) -> None:
    """Text deltas stream, id-less calls get ids, usage comes from the final chunk, and length stops are limits."""
    response = ollama_ndjson(
        {
            "model": "m",
            "created_at": "2026-10-20T00:00:00Z",
            "done": False,
            "message": {"role": "assistant", "content": "Hi"},
        },
        _ollama_done("length", tool_calls=[{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]),
    )

    _, events = await run_adapter(OLLAMA, _request(tools=[WEATHER]), tmp_path, response)

    completed = _completed(events)
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["Hi"]
    [call] = completed.message.tool_calls or []
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Paris"}'}
    assert call["id"]
    assert completed.message.metrics.tokens.input_tokens == 7
    assert completed.message.metrics.tokens.output_tokens == 5
    assert completed.stop_reason == "output_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "stop"),
    [
        ([_ollama_done(content="Hi")], "end"),
        ([_ollama_done(tool_calls=[{"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}])], "tool_use"),
        ([_ollama_done("load")], "incomplete"),
        # Servers from before done_reason existed, and some Ollama-compatible ones, end a normal reply without one.
        ([{key: value for key, value in _ollama_done(content="Hi").items() if key != "done_reason"}], "end"),
    ],
    ids=["stop", "stop-with-tool-calls", "load", "no-done-reason"],
)
async def test_ollama_stop_reasons(chunks: list[dict[str, Any]], stop: str, tmp_path: Path) -> None:
    """Done reasons map to stop reasons, and the done chunk reports the usage."""
    _, events = await run_adapter(OLLAMA, _request(tools=[WEATHER]), tmp_path, ollama_ndjson(*chunks))

    assert _completed(events).stop_reason == stop
    assert any(isinstance(event, UsageReported) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(404, json={"error": "model 'qwen' not found"}), InvalidRequest),
        (httpx.Response(500, json={"error": "llama runner process has terminated"}), ProviderUnavailable),
        (httpx.ConnectError("connection refused"), ProviderUnavailable),
        (ollama_ndjson({"error": "model runner has unexpectedly stopped"}), ProviderError),
    ],
)
async def test_ollama_errors_are_normalized(
    response: httpx.Response | Exception,
    error_type: type[ProviderError],
    tmp_path: Path,
) -> None:
    """Ollama response errors and connection failures become normalized provider errors."""
    with pytest.raises(ProviderError) as raised:
        await run_adapter(OLLAMA, _request(), tmp_path, response)

    assert type(raised.value) is error_type
    assert raised.value.provider == "Ollama"


@pytest.mark.asyncio
async def test_ollama_non_streaming_replies_split_think_tags(tmp_path: Path) -> None:
    """A reply that arrives whole has its ``<think>`` reasoning moved out of the content, as Agno did."""
    reply = httpx.Response(200, json=_ollama_done(content="<think>Plan first.</think>Hi there"))

    request, events = await run_adapter(OLLAMA, _request(stream=False), tmp_path, reply)

    assert request["body"]["stream"] is False
    completed = _completed(events)
    assert (completed.message.content, completed.message.reasoning_content) == ("Hi there", "Plan first.")
    assert [type(event).__name__ for event in events] == ["UsageReported", "TurnCompleted"]
    assert completed.message.metrics.tokens.input_tokens == 7


@pytest.mark.asyncio
async def test_ollama_non_streaming_tool_call_replies_have_no_text(tmp_path: Path) -> None:
    """A whole reply that only calls a tool finishes with the call and no empty text or reasoning."""
    reply = httpx.Response(
        200,
        json=_ollama_done(tool_calls=[{"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}]),
    )

    _, events = await run_adapter(OLLAMA, _request(tools=[WEATHER], stream=False), tmp_path, reply)

    assert [type(event).__name__ for event in events] == ["UsageReported", "TurnCompleted"]
    completed = _completed(events)
    assert (completed.message.content, completed.message.reasoning_content) == (None, None)
    [call] = completed.message.tool_calls or []
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Oslo"}'}
    assert completed.stop_reason == "tool_use"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["<think>\n\n</think>Hi there", "Plan first.</think>Hi there"],
    ids=["empty-reasoning", "no-opening-tag"],
)
async def test_ollama_keeps_think_tags_agno_did_not_split(content: str, tmp_path: Path) -> None:
    """Agno's Ollama split only an opening ``<think>`` with reasoning inside; other replies stay as they came."""
    reply = httpx.Response(200, json=_ollama_done(content=content))

    _, events = await run_adapter(OLLAMA, _request(stream=False), tmp_path, reply)

    completed = _completed(events)
    assert completed.message.content == content
    assert completed.message.reasoning_content is None


@pytest.mark.asyncio
async def test_ollama_replays_undecodable_tool_arguments_as_empty(tmp_path: Path) -> None:
    """Stored calls with broken or missing arguments replay as calls without arguments instead of failing."""
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "a", "type": "function", "function": {"name": "get_weather", "arguments": "{not json"}},
            {"id": "b", "type": "function", "function": {"name": "get_weather"}},
        ],
    )

    request, _ = await run_adapter(OLLAMA, _request(message), tmp_path, ollama_ndjson(_ollama_done()))

    assert request["body"]["messages"][0]["tool_calls"] == [
        {"function": {"name": "get_weather", "arguments": {}}},
        {"function": {"name": "get_weather", "arguments": {}}},
    ]


# ---------------------------------------------------------------------------
# Synthetic
# ---------------------------------------------------------------------------


def _synthetic_config(**extra_kwargs: object) -> ModelConfig:
    return ModelConfig(provider="synthetic", id="lorem-ipsum", extra_kwargs=extra_kwargs)


@pytest.mark.asyncio
async def test_synthetic_waits_for_each_chunk_at_the_configured_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each chunk waits for its character count at the configured fixed rate."""
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(synthetic_adapter.asyncio, "sleep", record_sleep)
    config = _synthetic_config(min_response_chars=10, max_response_chars=10, chunk_chars=4, chars_per_second=2)
    provider = build_synthetic_provider(config, runtime_paths_for(tmp_path))

    _completed([event async for event in provider.stream(_request())])

    assert sleeps == [2.0, 2.0, 1.0]


@pytest.mark.asyncio
async def test_a_whole_synthetic_reply_yields_only_its_finished_turn(tmp_path: Path) -> None:
    """``stream=False`` gets the planned text and shell call on the finished turn, without deltas, as on every route."""
    config = _synthetic_config(chars_per_second=0, tool_call_probability=1.0)
    provider = build_synthetic_provider(config, runtime_paths_for(tmp_path))
    shell = ToolSpec(name="run_shell_command", description="Run a shell command.", parameters={"type": "object"})

    [completed] = [event async for event in provider.stream(_request(tools=[shell], stream=False))]

    assert isinstance(completed, TurnCompleted)
    assert completed.message.content
    assert [call["function"]["name"] for call in completed.message.tool_calls or []] == ["run_shell_command"]


@pytest.mark.parametrize(
    ("extra_kwargs", "message"),
    [
        ({"min_response_chars": 1}, "at least 2"),
        ({"min_response_chars": 10, "max_response_chars": 9}, "greater than or equal"),
        ({"chunk_chars": 0}, "chunk_chars must be positive"),
        ({"chars_per_second": -1}, "chars_per_second must be non-negative"),
        ({"tool_call_probability": 1.1}, "between 0 and 1"),
        ({"temperature": 0.5}, "Unsupported extra_kwargs for provider 'synthetic': temperature"),
    ],
)
def test_synthetic_rejects_invalid_settings_at_build(
    extra_kwargs: dict[str, object],
    message: str,
    tmp_path: Path,
) -> None:
    """Invalid or unknown generation settings fail before serving any request."""
    with pytest.raises(ValueError, match=message):
        build_synthetic_provider(_synthetic_config(**extra_kwargs), runtime_paths_for(tmp_path))


def test_synthetic_ignores_an_api_key(tmp_path: Path) -> None:
    """A key the shared credential flow adds to every model does not break the synthetic model."""
    provider = build_synthetic_provider(_synthetic_config(api_key="unused", seed=4), runtime_paths_for(tmp_path))

    assert provider.settings.seed == 4
    assert (provider.identity.provider, provider.identity.provider_name) == ("Synthetic", "Synthetic")
