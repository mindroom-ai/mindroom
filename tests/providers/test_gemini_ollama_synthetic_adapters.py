"""Behavior of the owned Gemini, Ollama, and synthetic adapters that the C1 goldens do not exercise."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.config.models import ModelConfig
from mindroom.llm.errors import ContextWindowExceeded, InvalidRequest, ProviderError, ProviderUnavailable, RateLimited
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, TextDelta, ToolSpec, TurnCompleted
from mindroom.providers import synthetic_adapter
from mindroom.providers.gemini import adapter as gemini_adapter
from mindroom.providers.gemini import wire as gemini_wire
from mindroom.providers.gemini.wire import MALFORMED_FUNCTION_CALL_CODE
from mindroom.providers.ollama_adapter import build_provider as build_ollama_provider
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider
from mindroom.providers.tool_policy import without_provider_tools
from tests.providers.gemini_ollama_support import (
    gemini_sse,
    hide_provider_environment,
    ollama_ndjson,
    run_adapter,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent

GEMINI = {"provider": "google", "id": "gemini-2.5-pro", "api_key": "test-key"}
GEMINI_VERTEX = {
    "provider": "gemini",
    "id": "gemini-2.5-pro",
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


def test_gemini_rejects_unknown_and_dropped_extra_kwargs(tmp_path: Path) -> None:
    """Agno-only Gemini features fail at build time with the accepted keys listed."""
    config = ModelConfig(**_config(GEMINI, search=True))

    with pytest.raises(ValueError, match=r"Unsupported extra_kwargs for provider 'google': search\. Accepted keys: "):
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

    api = gemini_adapter.build_provider(ModelConfig(provider="gemini", id="gemini-2.5-pro"), runtime_paths)
    vertex = gemini_adapter.build_provider(ModelConfig(**GEMINI_VERTEX), runtime_paths)
    own = gemini_adapter.build_provider(ModelConfig(**GEMINI), runtime_paths)

    assert api.settings.api_key == "shared-key"
    assert vertex.settings.api_key is None
    assert vertex.vertexai
    assert own.settings.api_key == "test-key"
    assert requested == ["google"]


@pytest.mark.asyncio
async def test_gemini_settings_and_overrides_shape_the_generation_config(tmp_path: Path) -> None:
    """Authored settings reach the request; per-request overrides replace them and can drop thinking."""
    config = _config(
        GEMINI,
        generation_config={"candidate_count": 1},
        temperature=0.2,
        max_output_tokens=100,
        thinking_budget=512,
        include_thoughts=True,
    )
    response = gemini_sse(_gemini_chunk({"text": "ok"}))

    authored, _ = await run_adapter(config, _request(), tmp_path, response)
    overridden, _ = await run_adapter(
        config,
        _request(overrides=RequestOverrides(temperature=0.7, max_output_tokens=50, disable_thinking=True)),
        tmp_path,
        gemini_sse(_gemini_chunk({"text": "ok"})),
    )

    assert authored["body"]["generationConfig"] == {
        "candidateCount": 1,
        "temperature": 0.2,
        "maxOutputTokens": 100,
        "thinkingConfig": {"thinking_budget": 512, "include_thoughts": True},
    }
    assert overridden["body"]["generationConfig"] == {"candidateCount": 1, "temperature": 0.7, "maxOutputTokens": 50}


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
    if config is GEMINI_VERTEX:
        config = _config(config, credentials=_static_google_credentials())

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


def _static_google_credentials() -> object:
    from google.oauth2.credentials import Credentials  # noqa: PLC0415 - provider auth SDK

    return Credentials(token="test-google-token")  # noqa: S106 - synthetic credential


@pytest.mark.parametrize(("tool_choice", "mode"), [("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")])
def test_gemini_tool_choice_modes(tool_choice: str, mode: str) -> None:
    """The protocol's tool choices map to Gemini function-calling modes."""
    assert gemini_wire.tool_config(tool_choice) == {"function_calling_config": {"mode": mode}}


def test_gemini_rejects_a_named_tool_choice() -> None:
    """Gemini has no single-function choice in this adapter, so a mapping fails loudly."""
    with pytest.raises(ValueError, match="Gemini supports tool_choice"):
        gemini_wire.tool_config({"type": "function", "function": {"name": "get_weather"}})


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
    assert message.metrics.to_dict() | {"duration": None} == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "reasoning_tokens": 2,
        "duration": None,
    }
    assert completed.stop_reason == "tool_use"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish", "stop"),
    [("MAX_TOKENS", "output_limit"), ("SAFETY", "content_filter"), ("OTHER", "incomplete"), (None, None)],
)
async def test_gemini_stop_reasons(finish: str | None, stop: str | None, tmp_path: Path) -> None:
    """Gemini finish reasons map to the normalized stop reasons."""
    _, events = await run_adapter(GEMINI, _request(), tmp_path, gemini_sse(_gemini_chunk({"text": "x"}, finish=finish)))

    assert _completed(events).stop_reason == stop


@pytest.mark.asyncio
async def test_gemini_malformed_function_call_fails_with_its_code(tmp_path: Path) -> None:
    """A malformed function call ends the stream with an error the loop can answer with guidance."""
    response = gemini_sse(_gemini_chunk(finish="MALFORMED_FUNCTION_CALL"))

    with pytest.raises(ProviderError) as raised:
        await run_adapter(GEMINI, _request(), tmp_path, response)

    assert raised.value.code == MALFORMED_FUNCTION_CALL_CODE
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
async def test_gemini_transport_failure_is_retryable(tmp_path: Path) -> None:
    """A connection failure before any reply is a retryable provider error."""
    with pytest.raises(ProviderUnavailable) as raised:
        await run_adapter(GEMINI, _request(), tmp_path, httpx.ConnectError("connection refused"))

    assert raised.value.retryable


@pytest.mark.asyncio
async def test_gemini_inlines_local_media_and_skips_what_it_cannot_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local attachments are read and inlined; missing, oversized, and remote media are left out."""
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(PNG)
    large_path = tmp_path / "large.pdf"
    large_path.write_bytes(b"%PDF" + b"0" * 256)
    monkeypatch.setattr(gemini_wire, "_INLINE_MEDIA_LIMIT_BYTES", 128)
    message = Message(
        role="user",
        content="Look",
        images=[
            Image(filepath=str(image_path)),
            Image(filepath=str(tmp_path / "missing.png")),
            Image(url="https://example.com/a.png"),
        ],
        files=[File(filepath=str(large_path)), File(url="gs://bucket/notes.pdf")],
    )

    request, _ = await run_adapter(GEMINI, _request(message), tmp_path, gemini_sse(_gemini_chunk({"text": "ok"})))

    assert request["body"]["contents"][0]["parts"] == [
        {"text": "Look"},
        {"inlineData": {"data": base64.urlsafe_b64encode(PNG).decode(), "mimeType": "image/png"}},
        {"fileData": {"fileUri": "gs://bucket/notes.pdf", "mimeType": "application/pdf"}},
    ]


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
    original = {**schema, "properties": dict(schema["properties"])}

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

    assert provider.host == "http://localhost:11434"
    assert provider.settings.api_key == "env-key"


@pytest.mark.asyncio
async def test_ollama_native_settings_reach_the_chat_request(tmp_path: Path) -> None:
    """Options, keep_alive, format, and request_params pass through; overrides land in options."""
    config = _config(
        OLLAMA,
        api_key="ollama-key",
        options={"num_ctx": 8192},
        keep_alive="5m",
        format="json",
        request_params={"think": False},
    )

    request, _ = await run_adapter(
        config,
        _request(overrides=RequestOverrides(temperature=0.1, max_output_tokens=64)),
        tmp_path,
        ollama_ndjson(_ollama_done()),
    )

    body = request["body"]
    assert body["options"] == {"num_ctx": 8192, "temperature": 0.1, "num_predict": 64}
    assert body["keep_alive"] == "5m"
    assert body["format"] == "json"
    assert body["think"] is False
    assert request["url"] == "http://ollama.test:11434/api/chat"


@pytest.mark.asyncio
async def test_ollama_sends_the_api_key_as_a_bearer_token(tmp_path: Path) -> None:
    """An API key authenticates against Ollama's hosted endpoint."""
    seen: list[httpx.Request] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return ollama_ndjson(_ollama_done())

    config = ModelConfig(
        **_config(OLLAMA, api_key="ollama-key", client_params={"transport": httpx.MockTransport(respond)}),
    )
    provider = build_ollama_provider(config, runtime_paths_for(tmp_path))

    _completed([event async for event in provider.stream(_request())])

    assert seen[0].headers["authorization"] == "Bearer ollama-key"


@pytest.mark.asyncio
async def test_ollama_tools_are_sorted_and_leave_out_empty_descriptions(tmp_path: Path) -> None:
    """Tools go out sorted by name, and a tool without a description sends none, as Agno's did."""
    quiet = ToolSpec(name="a_tool", description="", parameters={"type": "object", "properties": {}})

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
    assert completed.message.metrics.input_tokens == 7
    assert completed.message.metrics.output_tokens == 5
    assert completed.stop_reason == "output_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(404, json={"error": "model 'qwen' not found"}), InvalidRequest),
        (httpx.Response(500, json={"error": "llama runner process has terminated"}), ProviderUnavailable),
        (httpx.ConnectError("connection refused"), ProviderUnavailable),
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
