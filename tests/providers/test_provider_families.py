"""Behavior every provider family must share: tool definitions, the composed pipeline, stream activity, finality.

Each family's bare adapter is built from a real model config over an httpx mock transport and wrapped in the
production pipeline (``wrap_provider``: the malformed-call guidance retry, portable tool search, media fallback,
retry with the idle limit, the safeguard refusal, request logging), so what these tests prove is what the registry
runs. The OpenAI family's idle-limit and
server-tool cases live in ``test_openai_family_wire.py``; the Claude tool tri-state in its behavior tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import INCOMPLETE_STREAM, MALFORMED_TOOL_CALL, ProviderError, ProviderUnavailable
from mindroom.llm.media import Audio, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, ToolSpec, complete
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.gemini.wire import MALFORMED_FUNCTION_CALL_GUIDANCE
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.media import FALLBACK_MARKER, forget_learned_media_limits
from mindroom.providers.pipeline.request_log import RequestLog
from tests.providers.anthropic_family_support import provider_for
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import PacedBytes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

# A Claude id without native tool search, so every family takes the portable tool-search projection.
_ANTHROPIC = {"provider": "anthropic", "id": "claude-3-7-sonnet-latest", "api_key": "test-key"}
_GEMINI = {"provider": "google", "id": "gemini-3.8-flash", "api_key": "test-key"}
_OLLAMA = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}
_OPENAI_CHAT = {"provider": "openai", "api": "chat_completions", "id": "gpt-6-astra", "api_key": "test-key"}
# A proxied Responses endpoint has no native tool search, so this family too takes the portable projection.
_OPENAI_RESPONSES = {
    "provider": "openai",
    "api": "responses",
    "id": "gpt-6-astra",
    "api_key": "test-key",
    "extra_kwargs": {"base_url": "https://proxy.example/v1"},
}
_CEREBRAS = {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "test-key"}
_PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "AWS_", "CLOUD_ML_", "GEMINI_", "GOOGLE_", "OLLAMA_", "OPENAI_", "CEREBRAS_")
_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}
_IDLE_LIMIT_SECONDS = 0.5
# Each pause stays far below the idle limit while the whole stream outlasts it.
_EVENT_PAUSE_SECONDS = 0.04
_SILENT_EVENTS = 25
_OVERLOADED = "The model is overloaded"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    forget_learned_media_limits()


class _Wire:
    """Records the requests one adapter sends and answers each from a queue; the last answer repeats."""

    def __init__(self, *responses: httpx.Response | Callable[[], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return response if isinstance(response, httpx.Response) else response()

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _streamed(chunks: list[bytes], content_type: str, *, pause_seconds: float, drop: bool) -> httpx.Response:
    headers = {"content-type": content_type}
    if pause_seconds or drop:
        return httpx.Response(200, headers=headers, stream=PacedBytes(chunks, pause_seconds=pause_seconds, drop=drop))
    return httpx.Response(200, headers=headers, content=b"".join(chunks))


def _sse(
    events: list[dict[str, Any] | str],
    *,
    named: bool = False,
    pause_seconds: float = 0.0,
    drop: bool = False,
) -> httpx.Response:
    chunks = [
        (
            f"data: {event}\n\n".encode()
            if isinstance(event, str)
            else (f"event: {event['type']}\n" if named else "").encode() + f"data: {json.dumps(event)}\n\n".encode()
        )
        for event in events
    ]
    return _streamed(chunks, "text/event-stream", pause_seconds=pause_seconds, drop=drop)


def _ndjson(events: list[dict[str, Any]], *, pause_seconds: float = 0.0, drop: bool = False) -> httpx.Response:
    chunks = [json.dumps(event).encode() + b"\n" for event in events]
    return _streamed(chunks, "application/x-ndjson", pause_seconds=pause_seconds, drop=drop)


# --- Per-family wire shapes -------------------------------------------------------------------------------------


def _claude_events(
    blocks: list[dict[str, Any]],
    *,
    deltas: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
) -> list[dict[str, Any]]:
    """A Claude stream: the start, each block whole (the last with ``deltas``), and the stop."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": _ANTHROPIC["id"],
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    for index, block in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        if index == len(blocks) - 1:
            events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas or [])
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
    )
    events.append({"type": "message_stop"})
    return events


def _gemini_chunk(*parts: dict[str, Any], finish: bool = False) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = "STOP"
    chunk: dict[str, Any] = {"candidates": [candidate]}
    if finish:
        chunk["usageMetadata"] = {"promptTokenCount": 40, "candidatesTokenCount": 9}
    return chunk


def _ollama_chunk(content: str = "", *, done: bool = False) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "model": _OLLAMA["id"],
        "created_at": "2026-10-20T00:00:00Z",
        "done": done,
        "message": {"role": "assistant", "content": content},
    }
    if done:
        chunk.update({"done_reason": "stop", "prompt_eval_count": 40, "eval_count": 9})
    return chunk


def _chat_chunk(delta: dict[str, Any] | None, *, finish_reason: str | None = None) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-6-astra",
    }
    if delta is None:
        chunk.update({"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}})
    else:
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    return chunk


def _responses_events(text: str, usage: dict[str, int]) -> list[dict[str, Any]]:
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra"}
    content = [{"type": "output_text", "text": text, "annotations": []}]
    output = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": content}]
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
            "logprobs": [],
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {**response, "status": "completed", "output": output, "usage": usage},
        },
    ]


def _claude_reply() -> httpx.Response:
    return _sse(_claude_events([{"type": "text", "text": "ok"}]), named=True)


def _gemini_reply() -> httpx.Response:
    return _sse([_gemini_chunk({"text": "ok"}, finish=True)])


def _ollama_reply() -> httpx.Response:
    return _ndjson([_ollama_chunk("ok"), _ollama_chunk(done=True)])


def _chat_reply() -> httpx.Response:
    return _sse([_chat_chunk({"content": "ok"}), _chat_chunk({}, finish_reason="stop"), _chat_chunk(None), "[DONE]"])


def _responses_reply() -> httpx.Response:
    return _sse(_responses_events("ok", {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}), named=True)


def _openai_family_adapter(config: dict[str, Any], root: Path, wire: _Wire) -> ModelProvider:
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={}),
        model_name="default",
        http_client=httpx.AsyncClient(transport=wire.transport),
    )
    return build_provider(ModelConfig(**config), runtime)


@dataclass(frozen=True)
class _Family:
    """How one family's adapter is built and how its wire shapes a request and a text reply."""

    config: dict[str, Any]
    reply: Callable[[], httpx.Response]
    # The function definitions as the wire sends them, keyed by tool name.
    wire_tools: Callable[[dict[str, Any]], dict[str, dict[str, Any]]]
    # The text of the last user message as the wire sends it.
    user_text: Callable[[dict[str, Any]], str] = lambda body: body["messages"][-1]["content"]
    # The media kind the route omits up front; empty when it omits none.
    omitted_media: str = ""

    def adapter(self, root: Path, wire: _Wire) -> ModelProvider:
        if self.config["provider"] == "anthropic":
            return provider_for(self.config, root, httpx.AsyncClient(transport=wire.transport))
        if self.config["provider"] in {"google", "ollama"}:
            return build_adapter(self.config, root, wire.respond)
        return _openai_family_adapter(self.config, root, wire)


def _chat_tools(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["function"]["name"]: tool["function"] for tool in body["tools"]}


FAMILIES = {
    "anthropic": _Family(
        _ANTHROPIC,
        _claude_reply,
        lambda body: {tool["name"]: tool for tool in body["tools"]},
        user_text=lambda body: body["messages"][-1]["content"][0]["text"],
        omitted_media="audio",
    ),
    "gemini": _Family(
        _GEMINI,
        _gemini_reply,
        lambda body: {tool["name"]: tool for tool in body["tools"][0]["functionDeclarations"]},
        user_text=lambda body: body["contents"][-1]["parts"][0]["text"],
    ),
    "ollama": _Family(_OLLAMA, _ollama_reply, _chat_tools, omitted_media="audio"),
    "openai_chat": _Family(_OPENAI_CHAT, _chat_reply, _chat_tools, omitted_media="video"),
    "openai_responses": _Family(
        _OPENAI_RESPONSES,
        _responses_reply,
        lambda body: {tool["name"]: tool for tool in body["tools"]},
        user_text=lambda body: body["input"][-1]["content"],
        omitted_media="video",
    ),
    "cerebras": _Family(_CEREBRAS, _chat_reply, _chat_tools, omitted_media="audio"),
}


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name}.", parameters=_SCHEMA)


def _pipeline(adapter: ModelProvider, *, log_dir: Path | None = None, idle: float | None = None) -> ModelProvider:
    log = None
    if log_dir is not None:
        log = RequestLog(
            model_name="default",
            configured_provider=adapter.identity.provider,
            debug=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(log_dir)),
            default_log_dir=log_dir / "unused",
        )
    return wrap_provider(
        adapter,
        idle_timeout_seconds=idle,
        media_fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT,
        request_log=log,
    )


# --- Tool definitions -------------------------------------------------------------------------------------------


_TRI_STATE_TOOLS = [
    ToolSpec(name="unset", description=None, parameters=_SCHEMA, strict=None),
    ToolSpec(
        name="gated",
        description="Gated.",
        parameters=_SCHEMA,
        strict=True,
        requires_confirmation=True,
        external_execution=True,
        approval_type="mindroom_policy",
    ),
    ToolSpec(
        name="plain",
        description="Plain.",
        parameters=_SCHEMA,
        strict=False,
        requires_confirmation=False,
        external_execution=False,
    ),
]
_GEMINI_SCHEMA = {"type": "OBJECT", "properties": {"city": {"type": "STRING"}}, "required": []}
_AGNO_FLAGS = {
    "gated": {
        "strict": True,
        "requires_confirmation": True,
        "external_execution": True,
        "approval_type": "mindroom_policy",
    },
    "plain": {"strict": False, "requires_confirmation": False, "external_execution": False},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["openai_chat", "openai_responses", "cerebras", "gemini", "ollama"])
async def test_unset_tool_fields_are_omitted_where_agno_omitted_them(family: str, tmp_path: Path) -> None:
    """``Function.to_dict()`` dropped None values; each wire keeps exactly the fields its Agno adapter sent.

    OpenAI's Chat and Responses wires carry ``strict`` and the approval flags when set; Cerebras and Ollama send
    only name, description, and parameters; Gemini sends an empty description for a tool without one.
    """
    spec = FAMILIES[family]
    wire = _Wire(spec.reply())
    provider = _pipeline(spec.adapter(tmp_path, wire))

    await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], tools=_TRI_STATE_TOOLS))

    sent = spec.wire_tools(wire.body())
    assert list(sent) == ["gated", "plain", "unset"]
    if family == "gemini":
        assert sent["unset"] == {"name": "unset", "description": "", "parameters": _GEMINI_SCHEMA}
        assert sent["gated"] == {"name": "gated", "description": "Gated.", "parameters": _GEMINI_SCHEMA}
        return
    kind = {"type": "function"} if family == "openai_responses" else {}
    assert sent["unset"] == {"name": "unset", "parameters": _SCHEMA, **kind}
    expected_flags: dict[str, dict[str, Any]] = (
        _AGNO_FLAGS if family in {"openai_chat", "openai_responses"} else {"gated": {}, "plain": {}}
    )
    for name in ("gated", "plain"):
        assert sent[name] == {
            "name": name,
            "description": f"{name.title()}.",
            "parameters": _SCHEMA,
            **expected_flags[name],
            **kind,
        }


# --- The composed pipeline --------------------------------------------------------------------------------------


def _error_reply(family: str) -> httpx.Response:
    bodies = {
        "anthropic": {"type": "error", "error": {"type": "api_error", "message": _OVERLOADED}},
        "gemini": {"error": {"code": 503, "message": _OVERLOADED, "status": "UNAVAILABLE"}},
        "ollama": {"error": _OVERLOADED},
    }
    return httpx.Response(503, json=bodies.get(family, {"error": {"message": _OVERLOADED}}))


def _media_message(spec: _Family, root: Path) -> Message:
    """A prompt with media the route omits up front, or an image the adapter cannot read where it omits none."""
    if spec.omitted_media == "audio":
        return Message(role="user", content="Look", audio=[Audio(content=b"RIFF", mime_type="audio/wav")])
    if spec.omitted_media == "video":
        return Message(role="user", content="Look", videos=[Video(content=b"\x00\x00", mime_type="video/mp4")])
    return Message(role="user", content="Look", images=[Image(filepath=str(root / "missing.png"))])


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_composed_pipeline_recovers_logs_and_projects_for_every_family(family: str, tmp_path: Path) -> None:
    """One request through every stage: media noted, deferred tools hidden, a 503 retried, the usage logged.

    Media the route omits is replaced by the fallback note up front; media the adapter cannot send (Gemini's
    unreadable image) is removed with the same note after its rejection. The deferred tool leaves the tool list
    and the request is reissued after the transient failure, with the request log recording every attempt.
    """
    spec = FAMILIES[family]
    wire = _Wire(_error_reply(family), spec.reply())
    log_dir = tmp_path / "logs"
    provider = _pipeline(spec.adapter(tmp_path, wire), log_dir=log_dir)
    request = ModelRequest(
        messages=[Message(role="system", content="Be brief."), _media_message(spec, tmp_path)],
        tools=[_tool("call_tool"), _tool("lookup_order"), _tool("tool_search")],
        deferred_tool_names=frozenset({"lookup_order"}),
        overrides=RequestOverrides(sdk_max_retries=0),
    )

    turn = await complete(provider, request)

    assert (turn.content, turn.stop_reason, len(wire.requests)) == ("ok", "end", 2)
    body = wire.body()
    assert sorted(spec.wire_tools(body)) == ["call_tool", "tool_search"]
    assert spec.user_text(body) == f"Look\n\n{FALLBACK_MARKER}\n{INLINE_MEDIA_FALLBACK_PROMPT}"
    serialized = json.dumps(body)
    assert "inlineData" not in serialized
    assert "input_audio" not in serialized
    assert "images" not in body
    (log_file,) = log_dir.glob("llm-requests-*.jsonl")
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    requests = [record for record in records if record.get("record") != "response"]
    (response,) = [record for record in records if record.get("record") == "response"]
    assert len(requests) == (3 if family == "gemini" else 2)
    assert [tool["name"] for tool in requests[-1]["tools"]] == ["call_tool", "tool_search"]
    assert FALLBACK_MARKER in json.dumps(requests[-1]["messages"])
    assert response["usage"] == {
        "input_tokens": 40,
        "output_tokens": 9,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert turn.usage.input_tokens == 40


# --- Stream activity and server tools ----------------------------------------------------------------------------


def _claude_argument_stream(*, pause_seconds: float) -> httpx.Response:
    arguments = json.dumps({"city": "P" * 50})
    step = -(-len(arguments) // _SILENT_EVENTS)
    pieces = [arguments[index : index + step] for index in range(0, len(arguments), step)]
    block = {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
    deltas = [{"type": "input_json_delta", "partial_json": piece} for piece in pieces]
    return _sse(_claude_events([block], deltas=deltas, stop_reason="tool_use"), named=True, pause_seconds=pause_seconds)


def _gemini_code_execution_stream(*, pause_seconds: float = 0.0, drop: bool = False) -> httpx.Response:
    running = _gemini_chunk({"executableCode": {"language": "PYTHON", "code": "print(1)"}})
    chunks: list[dict[str, Any] | str] = [running] * (_SILENT_EVENTS if not drop else 1)
    if not drop:
        chunks.append(_gemini_chunk({"text": "ok"}, finish=True))
    return _sse(chunks, pause_seconds=pause_seconds, drop=drop)


def _ollama_silent_stream(*, pause_seconds: float = 0.0, drop: bool = False) -> httpx.Response:
    chunks = [_ollama_chunk("")] * (_SILENT_EVENTS if not drop else 1)
    if not drop:
        chunks.extend([_ollama_chunk("ok"), _ollama_chunk(done=True)])
    return _ndjson(chunks, pause_seconds=pause_seconds, drop=drop)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "reply", "stop_reason"),
    [
        ("anthropic", lambda: _claude_argument_stream(pause_seconds=_EVENT_PAUSE_SECONDS), "tool_use"),
        ("gemini", lambda: _gemini_code_execution_stream(pause_seconds=_EVENT_PAUSE_SECONDS), "end"),
        ("ollama", lambda: _ollama_silent_stream(pause_seconds=_EVENT_PAUSE_SECONDS), "end"),
    ],
    ids=["anthropic-tool-arguments", "gemini-code-execution", "ollama-empty-chunks"],
)
async def test_provider_events_without_output_keep_a_stream_inside_the_idle_limit(
    family: str,
    reply: Callable[[], httpx.Response],
    stop_reason: str,
    tmp_path: Path,
) -> None:
    """The idle limit measures the gap between provider events, including those that carry no output."""
    spec = FAMILIES[family]
    wire = _Wire(spec.reply(), reply())
    provider = _pipeline(spec.adapter(tmp_path, wire), idle=_IDLE_LIMIT_SECONDS)
    request = ModelRequest(messages=[Message(role="user", content="Hi")], tools=[_tool("get_weather")])
    # The first request builds and caches the SDK client, which must not count against the paced stream's limit.
    await complete(provider, request)

    turn = await complete(provider, request)

    assert (turn.stop_reason, len(wire.requests)) == (stop_reason, 2)
    if family == "anthropic":
        assert turn.tool_calls[0]["function"]["arguments"] == json.dumps({"city": "P" * 50})


def _claude_server_tool_then_drop() -> httpx.Response:
    events = _claude_events([])[:1]
    block = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "paris"}}
    events.append({"type": "content_block_start", "index": 0, "content_block": block})
    return _sse(events, named=True, drop=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "reply"),
    [("anthropic", _claude_server_tool_then_drop), ("gemini", lambda: _gemini_code_execution_stream(drop=True))],
    ids=["anthropic-web-search", "gemini-code-execution"],
)
async def test_failure_after_a_server_tool_started_is_final(
    family: str,
    reply: Callable[[], httpx.Response],
    tmp_path: Path,
) -> None:
    """Once a hosted tool ran, a retry would run it again, so the dropped connection ends the request for good."""
    spec = FAMILIES[family]
    wire = _Wire(reply)
    provider = _pipeline(spec.adapter(tmp_path, wire))
    request = ModelRequest(messages=[Message(role="user", content="Hi")], overrides=RequestOverrides(sdk_max_retries=0))

    with pytest.raises(ProviderError) as raised:
        await complete(provider, request)

    assert (raised.value.code, raised.value.retryable, len(wire.requests)) == (INCOMPLETE_STREAM, False, 1)
    assert type(raised.value) is ProviderError


@pytest.mark.asyncio
async def test_a_malformed_function_call_does_not_strip_the_request_media(tmp_path: Path) -> None:
    """Gemini's MALFORMED_FUNCTION_CALL is the model's failure, not the media's: only the guidance retry follows."""
    spec = FAMILIES["gemini"]
    malformed = {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}]}
    wire = _Wire(lambda: _sse([malformed]))
    provider = _pipeline(spec.adapter(tmp_path, wire))
    image = Image(content=b"\x89PNG\r\n\x1a\n0000", mime_type="image/png")
    request = ModelRequest(
        messages=[Message(role="user", content="Look", images=[image])],
        tools=[_tool("get_weather")],
    )

    with pytest.raises(ProviderError) as raised:
        await complete(provider, request)

    assert (raised.value.code, raised.value.retryable, len(wire.requests)) == (MALFORMED_TOOL_CALL, False, 2)
    assert all("inlineData" in json.dumps(wire.body(index)) for index in range(2))
    texts = [part["text"] for content in wire.body()["contents"] for part in content["parts"] if "text" in part]
    assert texts[-1] == MALFORMED_FUNCTION_CALL_GUIDANCE


@pytest.mark.asyncio
async def test_a_dropped_connection_without_server_tools_is_still_retried(tmp_path: Path) -> None:
    """Ollama has no hosted tools, so a stream that drops before output keeps the transient retry budget."""
    spec = FAMILIES["ollama"]
    wire = _Wire(lambda: _ollama_silent_stream(drop=True))
    provider = _pipeline(spec.adapter(tmp_path, wire))

    with pytest.raises(ProviderUnavailable):
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")]))

    assert len(wire.requests) == 5
