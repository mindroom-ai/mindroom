"""Behavior every provider family must share: tool definitions, the composed pipeline, stream activity, finality.

Each family's bare adapter is built from a real model config over an httpx mock transport (the synthetic model
needs none) and wrapped in the production pipeline (``wrap_provider``: the malformed-call guidance retry, portable
tool search, media fallback, retry with the idle limit, the safeguard refusal, request logging), so what these tests
prove is what the registry runs. The OpenAI family's idle-limit and server-tool cases live in
``test_openai_family_wire.py``; the Claude tool tri-state in its behavior tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.types import HistoryScope
from mindroom.llm.errors import MALFORMED_TOOL_CALL, ProviderError, ProviderUnavailable
from mindroom.llm.media import Audio, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.provider import DeferredTools, ModelRequest, ToolSpec, complete
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.guidance_retry import MALFORMED_FUNCTION_CALL_GUIDANCE
from mindroom.providers.pipeline.media import FALLBACK_MARKER
from mindroom.providers.pipeline.request_log import RequestLog
from mindroom.providers.synthetic_adapter import SHELL_TOOL
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider
from mindroom.session_storage.codec import run_from_row
from tests.providers.family_support import (
    CLAUDE_DEPRECATION_FILTER,
    FAMILIES,
    Family,
    claude_events,
    gemini_chunk,
    ollama_chunk,
    scrub_provider_env,
)
from tests.providers.gemini_ollama_support import runtime_paths_for
from tests.providers.golden_support import Wire, ndjson_response, sse_response

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

pytestmark = pytest.mark.filterwarnings(CLAUDE_DEPRECATION_FILTER)

_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}
_IDLE_LIMIT_SECONDS = 0.5
# Each pause stays far below the idle limit while the whole stream outlasts it.
_EVENT_PAUSE_SECONDS = 0.04
_SILENT_EVENTS = 25
_OVERLOADED = "The model is overloaded"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    scrub_provider_env(monkeypatch)
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name}.", parameters=_SCHEMA)


def _pipeline(adapter: ModelProvider, *, log_dir: Path | None = None, idle: float | None = None) -> ModelProvider:
    log = None
    if log_dir is not None:
        log = RequestLog(
            model_name="default",
            configured_provider=adapter.identity.provider,
            debug=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(log_dir)),
            storage_root=log_dir / "unused",
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
    wire = Wire(spec.reply())
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


# --- Stored tool calls ------------------------------------------------------------------------------------------

# A thread that switched models replays tool calls other providers stored: a Chat call the output limit cut off
# mid-arguments, a Gemini call stored with empty arguments, a call stored without arguments by releases through
# v2026.9.43, and an id-only placeholder streamed Chat history kept through v2026.7.172, each with its result.
_STORED_CALLS = [
    {"id": "call_cut", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Par'}},
    {"id": "call_empty", "type": "function", "function": {"name": "get_weather", "arguments": ""}},
    {"id": "call_bare", "type": "function", "function": {"name": "get_weather"}},
    {"id": "call_placeholder"},
]
_STORED_RUN = {
    "run_id": "run-1",
    "agent_id": "code",
    "status": "COMPLETED",
    "messages": [
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": _STORED_CALLS},
        *(
            {"role": "tool", "tool_call_id": call["id"], "tool_name": "get_weather", "content": "sunny"}
            for call in _STORED_CALLS
        ),
    ],
}


def _stored_tool_turn() -> list[Message]:
    """The request a new prompt sends after the stored run, assembled from its row as a reply assembles it."""
    run = run_from_row("agent", json.dumps(_STORED_RUN))
    assert run is not None
    history = scope_history_messages(
        [run],
        scope=HistoryScope(kind="agent", scope_id="code"),
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    prompt = Message(role="user", content="And tomorrow?")
    return build_request(system=(), summary=None, history=history, turn=TurnMessages(prompt=prompt))


def _replayed_tool_turn(family: str, body: dict[str, Any]) -> tuple[list[object], int]:
    """Return the arguments of every tool call the request replays, in order, and how many results it replays."""
    if family == "anthropic":
        blocks = [block for message in body["messages"] for block in message["content"] if isinstance(block, dict)]
        calls = [block["input"] for block in blocks if block["type"] == "tool_use"]
        return calls, sum(block["type"] == "tool_result" for block in blocks)
    if family.startswith("gemini"):
        parts = [part for content in body["contents"] for part in content["parts"]]
        calls = [part["functionCall"].get("args") for part in parts if "functionCall" in part]
        return calls, sum("functionResponse" in part for part in parts)
    if family == "openai_responses":
        calls = [item["arguments"] for item in body["input"] if item.get("type") == "function_call"]
        return calls, sum(item.get("type") == "function_call_output" for item in body["input"])
    messages = body["messages"]
    calls = [call["function"]["arguments"] for message in messages for call in message.get("tool_calls") or ()]
    return calls, sum(message["role"] == "tool" for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_every_family_replays_the_tool_calls_any_provider_stored(family: str, tmp_path: Path) -> None:
    """Every stored call shape replays on every route: the placeholder and its result are dropped everywhere.

    Routes that send arguments as text replay the stored text, with ``{}`` for a call stored without any; routes
    that send an object replay ``{}`` for arguments that are not one.
    """
    spec = FAMILIES[family]
    wire = Wire(spec.reply())

    await complete(
        _pipeline(spec.adapter(tmp_path, wire)),
        ModelRequest(messages=_stored_tool_turn(), tools=[_tool("get_weather")]),
    )

    body = wire.body()
    assert "call_placeholder" not in json.dumps(body)
    as_text = family in {"openai_chat", "openai_responses", "cerebras", "groq"}
    assert _replayed_tool_turn(family, body) == (['{"city": "Par', "", "{}"] if as_text else [{}, {}, {}], 3)


# --- The composed pipeline --------------------------------------------------------------------------------------


def _without_sdk_retries(spec: Family) -> Family:
    """Return ``spec`` with the SDK's own HTTP retries authored off, so the pipeline alone reissues a request.

    The Gemini and Ollama SDKs retry only when a config authors it.
    """
    if spec.config["provider"] in {"google", "ollama"}:
        return spec
    extra_kwargs = {**spec.config.get("extra_kwargs", {}), "client_params": {"max_retries": 0}}
    return replace(spec, config={**spec.config, "extra_kwargs": extra_kwargs})


def _error_reply(family: str) -> httpx.Response:
    gemini = {"error": {"code": 503, "message": _OVERLOADED, "status": "UNAVAILABLE"}}
    bodies = {
        "anthropic": {"type": "error", "error": {"type": "api_error", "message": _OVERLOADED}},
        "gemini": gemini,
        "gemini_vertexai": gemini,
        "ollama": {"error": _OVERLOADED},
    }
    return httpx.Response(503, json=bodies.get(family, {"error": {"message": _OVERLOADED}}))


def _media_message(spec: Family, root: Path) -> Message:
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

    Media the route omits is replaced by the fallback note up front; media the adapter cannot send (an unreadable
    image on Gemini and on Vertex AI) is removed with the same note after its rejection. The deferred tool leaves
    the tool list and the request is reissued after the transient failure, with the request log recording every
    attempt.
    """
    spec = _without_sdk_retries(FAMILIES[family])
    wire = Wire(_error_reply(family), spec.reply())
    log_dir = tmp_path / "logs"
    provider = _pipeline(spec.adapter(tmp_path, wire), log_dir=log_dir)
    request = ModelRequest(
        messages=[Message(role="system", content="Be brief."), _media_message(spec, tmp_path)],
        tools=[_tool("call_tool"), _tool("lookup_order"), _tool("tool_search")],
        deferred_tools=DeferredTools(names=frozenset({"lookup_order"}), portable=True),
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
    # Gemini's rejected image is one more logged attempt, which never reaches the wire.
    assert len(requests) == (3 if family.startswith("gemini") else 2)
    assert [tool["name"] for tool in requests[-1]["tools"]] == ["call_tool", "tool_search"]
    assert FALLBACK_MARKER in json.dumps(requests[-1]["messages"])
    assert response["usage"] == {
        "input_tokens": 40,
        "output_tokens": 9,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert turn.usage.input_tokens == 40


@pytest.mark.asyncio
async def test_composed_pipeline_serves_the_synthetic_family(tmp_path: Path) -> None:
    """The synthetic model has no wire, but every stage still applies to it as the registry composes it.

    The deferred tool leaves the tool list, the shell call and the continuation after its result pass through
    the media, retry, and logging stages, and each request is logged; a synthetic reply reports no usage, so,
    as on the Agno path, no response record follows.
    """
    settings = {"min_response_chars": 200, "max_response_chars": 200, "chars_per_second": 0, "tool_call_probability": 1}
    adapter = build_synthetic_provider(
        ModelConfig(provider="synthetic", id="lorem-ipsum", extra_kwargs=settings),
        runtime_paths_for(tmp_path),
    )
    log_dir = tmp_path / "logs"
    provider = _pipeline(adapter, log_dir=log_dir, idle=_IDLE_LIMIT_SECONDS)
    request = ModelRequest(
        messages=[Message(role="user", content="Hi")],
        tools=[_tool("call_tool"), _tool("lookup_order"), _tool(SHELL_TOOL), _tool("tool_search")],
        deferred_tools=DeferredTools(names=frozenset({"lookup_order"}), portable=True),
    )

    calling = await complete(provider, request)
    (call,) = calling.tool_calls
    result = Message(role="tool", content="hi", tool_call_id=call["id"], tool_name=SHELL_TOOL)
    answer = await complete(provider, replace(request, messages=[*request.messages, calling.message, result]))

    assert (calling.stop_reason, call["function"]["name"], answer.stop_reason) == ("tool_use", SHELL_TOOL, "end")
    assert len(calling.content + answer.content) == 200
    (log_file,) = log_dir.glob("llm-requests-*.jsonl")
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [record.get("record") for record in records] == [None, None]
    assert [[tool["name"] for tool in record["tools"]] for record in records] == [
        ["call_tool", SHELL_TOOL, "tool_search"],
    ] * 2
    assert records[1]["messages"][-1]["tool_call_id"] == call["id"]


# --- Timings ------------------------------------------------------------------------------------------------------


_REPLIES_WITHOUT_OUTPUT: dict[str, Callable[[], httpx.Response]] = {
    "openai_chat": lambda: sse_response(
        [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [],
                "usage": {"prompt_tokens": 40, "completion_tokens": 0, "total_tokens": 40},
            },
            "[DONE]",
        ],
    ),
    "gemini": lambda: sse_response([gemini_chunk(finish=True)]),
    "ollama": lambda: ndjson_response([ollama_chunk(done=True)]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _REPLIES_WITHOUT_OUTPUT)
async def test_a_reply_without_output_records_no_time_to_first_token(family: str, tmp_path: Path) -> None:
    """Only text, reasoning, or a tool call is output; a chunk that carries only usage starts no first-token time."""
    spec = FAMILIES[family]
    wire = Wire(_REPLIES_WITHOUT_OUTPUT[family]())

    turn = await complete(
        _pipeline(spec.adapter(tmp_path, wire)),
        ModelRequest(messages=[Message(role="user", content="Hi")]),
    )

    assert turn.content == ""
    assert turn.usage.time_to_first_token is None


# --- Stream activity and server tools ----------------------------------------------------------------------------


def _claude_argument_stream(*, pause_seconds: float) -> httpx.Response:
    arguments = json.dumps({"city": "P" * 50})
    step = -(-len(arguments) // _SILENT_EVENTS)
    pieces = [arguments[index : index + step] for index in range(0, len(arguments), step)]
    block = {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
    deltas = [{"type": "input_json_delta", "partial_json": piece} for piece in pieces]
    return sse_response(
        claude_events([block], deltas=deltas, stop_reason="tool_use"),
        named=True,
        pause_seconds=pause_seconds,
    )


def _gemini_code_execution_stream(*, pause_seconds: float = 0.0, drop: bool = False) -> httpx.Response:
    running = gemini_chunk({"executableCode": {"language": "PYTHON", "code": "print(1)"}})
    chunks: list[dict[str, Any] | str] = [running] * (_SILENT_EVENTS if not drop else 1)
    if not drop:
        chunks.append(gemini_chunk({"text": "ok"}, finish=True))
    return sse_response(chunks, pause_seconds=pause_seconds, drop=drop)


def _ollama_silent_stream(*, pause_seconds: float = 0.0, drop: bool = False) -> httpx.Response:
    chunks = [ollama_chunk("")] * (_SILENT_EVENTS if not drop else 1)
    if not drop:
        chunks.extend([ollama_chunk("ok"), ollama_chunk(done=True)])
    return ndjson_response(chunks, pause_seconds=pause_seconds, drop=drop)


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
    wire = Wire(spec.reply(), reply())
    provider = _pipeline(spec.adapter(tmp_path, wire), idle=_IDLE_LIMIT_SECONDS)
    request = ModelRequest(messages=[Message(role="user", content="Hi")], tools=[_tool("get_weather")])
    # The first request builds and caches the SDK client, which must not count against the paced stream's limit.
    await complete(provider, request)

    turn = await complete(provider, request)

    assert (turn.stop_reason, len(wire.requests)) == (stop_reason, 2)
    if family == "anthropic":
        assert turn.tool_calls[0]["function"]["arguments"] == json.dumps({"city": "P" * 50})


def _claude_server_tool_then_drop() -> httpx.Response:
    events = claude_events([])[:1]
    block = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "paris"}}
    events.append({"type": "content_block_start", "index": 0, "content_block": block})
    return sse_response(events, named=True, drop=True)


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
    wire = Wire(lambda _request: reply())
    provider = _pipeline(spec.adapter(tmp_path, wire))
    request = ModelRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(ProviderUnavailable) as raised:
        await complete(provider, request)

    assert (raised.value.code, len(wire.requests)) == ("connection_error", 1)


@pytest.mark.asyncio
async def test_a_malformed_function_call_does_not_strip_the_request_media(tmp_path: Path) -> None:
    """Gemini's MALFORMED_FUNCTION_CALL is the model's failure, not the media's: only the guidance retry follows."""
    spec = FAMILIES["gemini"]
    malformed = {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}]}
    wire = Wire(lambda _request: sse_response([malformed]))
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
    wire = Wire(lambda _request: _ollama_silent_stream(drop=True))
    provider = _pipeline(spec.adapter(tmp_path, wire))

    with pytest.raises(ProviderUnavailable):
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")]))

    assert len(wire.requests) == 5
