"""What every family's provider-golden harness shares: loading, the owned request, the pipeline, normalization.

A golden (``tests/fixtures/agno_parity/providers/<family>/<scenario>.json``, format in the README beside them)
pins one request the owned adapter sends and the assistant message it parses, first captured from the Agno-backed
provider layer and now regenerated from the owned one (``tests.agno_parity.regenerate_goldens``). Each family
builds its own adapter from the golden's model config and answers its HTTP traffic with the canned reply; this
module turns the golden input into the owned ``ModelRequest``, runs it through the production pipeline stages
(portable tool search, media fallback, retry, request log), and records what came out as the goldens hold it: the
wire request, and the reply as a provider-neutral turn (``regenerated_golden``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
from pydantic import BaseModel

from mindroom.config.models import DebugConfig
from mindroom.llm.messages import Message
from mindroom.llm.provider import DeferredTools, ModelRequest, ToolSpec, TurnCompleted
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.request_log import RequestLog
from mindroom.tool_jobs.execution import project_definitions
from tests.agno_parity.golden_files import STORAGE_ONLY_MESSAGE_KEYS, sorted_keys, turn_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    import pytest

    from mindroom.llm.provider import ModelProvider, NativeHistoryRoute, StreamEvent

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"

# Provider SDKs read credentials and endpoints from these environment variables when a config leaves them unset.
_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CEREBRAS_",
    "CLOUD_ML_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "GROQ_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
)
# google-auth falls back to gcloud's well-known credentials file when GOOGLE_APPLICATION_CREDENTIALS is unset, and
# refreshes what it finds through ``requests``, which the network guard does not cover. A path that cannot exist
# makes every Application Default Credentials lookup raise ``DefaultCredentialsError`` instead.
_MISSING_GOOGLE_CREDENTIALS = str(Path(os.devnull) / "application_default_credentials.json")

# Headers the SDK or transport derives from the environment, the credentials, or the body length.
_VOLATILE_HEADER_PREFIXES = ("x-stainless-", "x-amz-", "amz-sdk-")
_VOLATILE_HEADERS = frozenset(
    {
        "accept-encoding",
        "api-key",
        "authorization",
        "connection",
        "content-length",
        "host",
        "user-agent",
        "x-api-key",
        "x-goog-api-client",
        "x-goog-api-key",
    },
)
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
GENERATED_ID = "<uuid4>"
# A request as an httpx or an httpx2 client sent it; both packages give it the same fields.
type SentRequest = httpx.Request | httpx2.Request
_TIMING_METRIC_KEYS = ("duration", "time_to_first_token")
# Every golden input key the harness turns into the owned request; a new key fails loudly instead of silently.
_HANDLED_INPUT_KEYS = frozenset(
    {
        "deferred_tool_search",
        "messages",
        "model_config",
        "native_compaction",
        "output_schema",
        "portable_replay",
        "tool_choice",
        "tool_jobs",
        "tools",
    },
)


def hide_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's provider credentials and endpoints from SDK fallbacks, and fail Google ADC lookups closed."""
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", _MISSING_GOOGLE_CREDENTIALS)


class PacedBytes(httpx.AsyncByteStream):
    """Response body chunks sent one at a time after a pause each, optionally ending in a dropped connection."""

    def __init__(self, chunks: Sequence[bytes], *, pause_seconds: float = 0.0, drop: bool = False) -> None:
        self._chunks = list(chunks)
        self._pause_seconds = pause_seconds
        self._drop = drop

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield each chunk after its pause, then drop the connection when asked."""
        for chunk in self._chunks:
            await asyncio.sleep(self._pause_seconds)
            yield chunk
        if self._drop:
            msg = "connection dropped"
            raise httpx.ReadError(msg)


def _httpx2_error(error: httpx.TransportError) -> httpx2.TransportError:
    """Return the httpx2 error of the same name, the one an httpx2 transport raises for this failure.

    Raise it ``from None``: the SDKs see only httpx2 errors, so classification must not find the httpx one too.
    """
    return getattr(httpx2, type(error).__name__)(str(error))


class _Httpx2Body(httpx2.AsyncByteStream):
    """An httpx reply body read through httpx2, its transport failure raised as the httpx2 one."""

    def __init__(self, body: httpx.AsyncByteStream) -> None:
        self._body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the body's chunks."""
        try:
            async for chunk in self._body:
                yield chunk
        except httpx.TransportError as error:
            raise _httpx2_error(error) from None

    async def aclose(self) -> None:
        """Close the body."""
        await self._body.aclose()


def httpx2_transport(handler: Callable[[httpx2.Request], httpx.Response | httpx2.Response]) -> httpx2.MockTransport:
    """Answer the httpx2 requests the OpenAI and Claude SDKs send with ``handler``'s httpx replies and failures.

    The reply builders here and the Gemini and Ollama SDKs use httpx, and httpx2 is its fork with the same API, so a
    reply converts by its fields and a transport failure by its class name.
    """

    def respond(request: httpx2.Request) -> httpx2.Response:
        try:
            reply = handler(request)
        except httpx.TransportError as error:
            raise _httpx2_error(error) from None
        if isinstance(reply, httpx2.Response):
            return reply
        headers = reply.headers.multi_items()
        if isinstance(reply.stream, httpx.ByteStream):
            return httpx2.Response(reply.status_code, headers=headers, content=reply.read())
        return httpx2.Response(reply.status_code, headers=headers, stream=_Httpx2Body(reply.stream))

    return httpx2.MockTransport(respond)


class Wire:
    """Records the requests providers send and answers each from a queue; the last answer repeats.

    A callable answer is called with the request, for replies that depend on it or must be built fresh.
    ``client`` is the httpx2 client OpenAI and Claude models send through, and ``transport`` the httpx transport
    Gemini and Ollama SDK clients take.
    """

    def __init__(self, *replies: httpx.Response | Callable[[SentRequest], httpx.Response]) -> None:
        self.requests: list[SentRequest] = []
        self.replies = list(replies)
        self.transport = httpx.MockTransport(self.respond)
        self.client = httpx2.AsyncClient(transport=httpx2_transport(self.respond))

    def respond(self, request: SentRequest) -> httpx.Response:
        """Record ``request`` and return the next answer."""
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, httpx.Response) else reply(request)

    def body(self, index: int = -1) -> dict[str, Any]:
        """Return the JSON body of one recorded request, the last by default."""
        return json.loads(self.requests[index].content)


def _streamed(
    chunks: list[bytes],
    content_type: str,
    *,
    status: int,
    pause_seconds: float,
    drop: bool,
) -> httpx.Response:
    headers = {"content-type": content_type}
    if pause_seconds or drop:
        return httpx.Response(
            status,
            headers=headers,
            stream=PacedBytes(chunks, pause_seconds=pause_seconds, drop=drop),
        )
    return httpx.Response(status, headers=headers, content=b"".join(chunks))


def sse_response(
    events: Sequence[dict[str, Any] | str],
    *,
    named: bool = False,
    status: int = 200,
    pause_seconds: float = 0.0,
    drop: bool = False,
) -> httpx.Response:
    """Return a server-sent event stream: each dict as JSON data (``named`` adds its ``type`` as the event), a string as is.

    ``pause_seconds`` sends each event after a pause, and ``drop`` ends the stream with a transport error.
    """
    chunks = [
        (
            f"data: {event}\n\n".encode()
            if isinstance(event, str)
            else (f"event: {event['type']}\n" if named else "").encode() + f"data: {json.dumps(event)}\n\n".encode()
        )
        for event in events
    ]
    return _streamed(chunks, "text/event-stream", status=status, pause_seconds=pause_seconds, drop=drop)


def ndjson_response(
    events: Sequence[dict[str, Any]],
    *,
    status: int = 200,
    pause_seconds: float = 0.0,
    drop: bool = False,
) -> httpx.Response:
    """Return a newline-delimited JSON stream, as Ollama sends; the options are those of ``sse_response``."""
    chunks = [json.dumps(event).encode() + b"\n" for event in events]
    return _streamed(chunks, "application/x-ndjson", status=status, pause_seconds=pause_seconds, drop=drop)


def streamed_reply(reply: dict[str, Any]) -> httpx.Response:
    """Return the stream a route sends for one finished reply body.

    ``reply`` is a Chat completion, a Responses response, a Claude message, a Gemini response, or Ollama's done chunk.
    """
    if "choices" in reply:
        return sse_response([*_chat_chunks(reply), "[DONE]"])
    if reply.get("object") == "response":
        return sse_response(responses_events(reply), named=True)
    if reply.get("type") == "message":
        return sse_response(claude_stream_events(reply), named=True)
    if "candidates" in reply or "promptFeedback" in reply:
        return sse_response([reply])
    return ndjson_response([reply])


def _chat_chunks(completion: dict[str, Any]) -> list[dict[str, Any]]:
    choice = completion["choices"][0]
    base = {key: value for key, value in completion.items() if key not in {"choices", "usage"}}
    base["object"] = "chat.completion.chunk"
    delta = {key: value for key, value in choice["message"].items() if key != "tool_calls" and value is not None}
    if calls := choice["message"].get("tool_calls"):
        delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(calls)]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
    ]
    if (usage := completion.get("usage")) is not None:
        chunks.append({**base, "choices": [], "usage": usage})
    return chunks


def responses_events(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the events the Responses API streams for ``response``: its start, each output item, then its end."""
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {**response, "output": [], "status": "in_progress", "usage": None}},
    ]
    for index, item in enumerate(response["output"]):
        added = {**item, "arguments": ""} if item["type"] == "function_call" else item
        events.append({"type": "response.output_item.added", "output_index": index, "item": added})
        common = {"item_id": item.get("id"), "output_index": index, "content_index": 0}
        if item["type"] == "function_call":
            events.append({"type": "response.function_call_arguments.delta", **common, "delta": item["arguments"]})
        if item["type"] == "message":
            texts = [part["text"] for part in item["content"]]
            events.extend(
                {"type": "response.output_text.delta", **common, "delta": text, "logprobs": []} for text in texts
            )
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    ended = "response.completed" if response["status"] == "completed" else "response.incomplete"
    events.append({"type": ended, "response": response})
    return [{**event, "sequence_number": number} for number, event in enumerate(events)]


def claude_stream_events(
    message: dict[str, Any],
    *,
    context_management: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the events the Messages API streams for ``message``: a start, deltas per block, the stop reason, the stop.

    A ``tool_use`` block may name the ``partial_json`` its arguments stream as, and another block the ``deltas`` it
    streams; both helper-only keys stay out of the streamed block. ``context_management`` is the context editing the
    response applied, which arrives with the stop reason.
    """
    usage = message["usage"]
    start = {**message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 1}}
    events: list[dict[str, Any]] = [{"type": "message_start", "message": start}]
    for index, authored in enumerate(message["content"]):
        # Popping the helper-only keys below must leave the caller's blocks reusable.
        block = dict(authored)
        if block["type"] == "text":
            opened, deltas = {**block, "text": ""}, [{"type": "text_delta", "text": block["text"]}]
        elif block["type"] == "tool_use":
            partial_json = block.pop("partial_json", None)
            arguments = json.dumps(block["input"]) if partial_json is None else partial_json
            opened, deltas = {**block, "input": {}}, [{"type": "input_json_delta", "partial_json": arguments}]
        elif block["type"] == "thinking":
            opened = {**block, "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        else:
            # Other blocks arrive whole, unless the block names deltas of its own.
            deltas = block.pop("deltas", [])
            opened = block
        events.append({"type": "content_block_start", "index": index, "content_block": opened})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    stop = {"stop_reason": message["stop_reason"], "stop_sequence": message.get("stop_sequence")}
    applied = {} if context_management is None else {"context_management": context_management}
    events.append(
        {"type": "message_delta", "delta": stop, "usage": {"output_tokens": usage["output_tokens"]}, **applied},
    )
    events.append({"type": "message_stop"})
    return events


def encoded_response(response_raw: dict[str, Any]) -> httpx.Response:
    """Build the HTTP response a golden's ``response_raw`` describes: an SSE or ndjson stream, or a JSON body.

    Tests may add ``pause_seconds`` to send each event after a pause, and ``drop_connection`` to end the stream
    with a transport error instead of EOF.
    """
    status, headers = response_raw["status"], response_raw["headers"]
    if "json" in response_raw:
        return httpx.Response(status, headers=headers, content=json.dumps(response_raw["json"]).encode())
    if "ndjson" in response_raw:
        chunks = [json.dumps(event).encode() + b"\n" for event in response_raw["ndjson"]]
    else:
        chunks = [
            (f"event: {event['event']}\n" if "event" in event else "").encode()
            + f"data: {data if isinstance(data := event['data'], str) else json.dumps(data)}\n\n".encode()
            for event in response_raw["sse"]
        ]
    if "pause_seconds" in response_raw or "drop_connection" in response_raw:
        stream = PacedBytes(
            chunks,
            pause_seconds=response_raw.get("pause_seconds", 0.0),
            drop=response_raw.get("drop_connection", False),
        )
        return httpx.Response(status, headers=headers, stream=stream)
    return httpx.Response(status, headers=headers, content=b"".join(chunks))


@dataclass(frozen=True)
class Replay:
    """What the owned adapter sent and produced for one golden."""

    request: dict[str, Any]
    parsed: dict[str, Any]
    events: list[StreamEvent]


def golden_paths(families: Sequence[str]) -> list[Path]:
    """Return every golden of the given families, in a stable order."""
    return sorted(path for family in families for path in (FIXTURE_ROOT / family).glob("*.json"))


def golden_id(path: Path) -> str:
    """Return the ``family/scenario`` id of one golden."""
    return f"{path.parent.name}/{path.stem}"


def load_golden(path: Path) -> dict[str, Any]:
    """Read one golden file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _json_roundtrip(value: object) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads(json.dumps(value, sort_keys=True))


def scrub_generated_ids(value: object) -> Any:  # noqa: ANN401 - arbitrary JSON
    """Replace uuid4 strings, which adapters generate for id-less tool calls, by the golden placeholder."""
    if isinstance(value, dict):
        return {key: scrub_generated_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_generated_ids(item) for item in value]
    if isinstance(value, str) and _UUID4.match(value):
        return GENERATED_ID
    return value


def normalized_request(request: SentRequest) -> dict[str, Any]:
    """Return the method, URL, stable headers, and JSON body of one SDK request, as the provider goldens record them."""
    headers = {
        name: value
        for name, value in sorted(request.headers.items())
        if name not in _VOLATILE_HEADERS and not name.startswith(_VOLATILE_HEADER_PREFIXES)
    }
    return {
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
        "body": json.loads(request.content) if request.content else None,
    }


def parsed_turn(completed: TurnCompleted) -> dict[str, Any]:
    """Return a finished turn as the goldens record it: its message as a turn, its usage, and its stop reason."""
    message = turn_message(completed.message)
    metrics = message.pop("metrics", {})
    for key in _TIMING_METRIC_KEYS:
        metrics.pop(key, None)
    parsed = {"message": message, "metrics": metrics, "stop_reason": completed.stop_reason}
    return scrub_generated_ids(_json_roundtrip(parsed))


def regenerated_golden(golden: dict[str, Any], replay: Replay) -> dict[str, Any]:
    """Return the golden that ``replay`` of ``golden`` regenerates.

    The scenario, input, and canned reply stay as authored; the request is the one the owned adapter sent and the
    reply the one it parsed. Every key is in sorted order except the request body's, which keeps the order the SDK
    sent, because chat templates and prompt caches see it.
    """
    request = sorted_keys({key: value for key, value in replay.request.items() if key != "body"})
    request["body"] = replay.request["body"]
    return {
        "family": golden["family"],
        "input": golden["input"],
        "parsed": replay.parsed,
        "request": dict(sorted(request.items())),
        "response_raw": golden["response_raw"],
        "scenario": golden["scenario"],
    }


def comparable_metrics(metrics: Any) -> dict[str, Any]:  # noqa: ANN401 - MessageMetrics
    """Return a usage report without the wall-clock timings the goldens drop."""
    values = metrics.to_dict()
    for key in _TIMING_METRIC_KEYS:
        values.pop(key, None)
    return values


def output_schema_from_golden(encoded: dict[str, Any] | None) -> type[BaseModel] | None:
    """Return the output schema a golden's request asks for, which must still have the JSON schema it recorded."""
    if encoded is None:
        return None
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    assert issubclass(schema, BaseModel)
    if schema.model_json_schema() != encoded["json_schema"]:
        msg = f"{schema.__name__} no longer matches the schema recorded in the golden"
        raise AssertionError(msg)
    return schema


def golden_request(case_input: dict[str, Any], *, native: NativeHistoryRoute | None = None) -> ModelRequest:
    """Return the owned request a golden's input describes.

    Deferred tools travel on the request in the golden's recorded mode: the portable tool-search stage hides
    portable ones behind ``call_tool``, and the adapters defer native ones server-side.
    A native-compaction input becomes the replay settings of the route's ``native`` history, as history planning
    asks for them.
    """
    unhandled = set(case_input) - _HANDLED_INPUT_KEYS
    assert not unhandled, f"the harness does not know how to send golden input {sorted(unhandled)}"
    tools = [ToolSpec(**tool) for tool in case_input["tools"]]
    if (tool_jobs := case_input.get("tool_jobs")) is not None:
        tools = list(project_definitions(tools, frozenset(tool_jobs["managed"])))
    native_compaction = None
    if (asked := case_input.get("native_compaction")) is not None:
        assert native is not None, "the golden's route has no native compaction"
        native_compaction = native.native_compaction_settings(
            threshold=asked["threshold"],
            history_generation=asked["history_generation"],
            allow_authored=True,
        )
        assert native_compaction is not None, "the golden's route supports native compaction"
    deferred = case_input["deferred_tool_search"]
    tool_choice = case_input["tool_choice"]
    assert tool_choice in (None, "none"), "the owned loop sends tool_choice only as None or 'none'"
    stored = [sorted(set(message) & STORAGE_ONLY_MESSAGE_KEYS) for message in case_input["messages"]]
    assert not any(stored), f"golden input messages are turns, without the storage-only keys {stored}"
    return ModelRequest(
        messages=[Message.from_dict(deepcopy(message)) for message in case_input["messages"]],
        tools=tools,
        tool_choice=tool_choice,
        output_schema=output_schema_from_golden(case_input["output_schema"]),
        deferred_tools=DeferredTools()
        if deferred is None
        else DeferredTools(names=frozenset(deferred["tool_names"]), portable=deferred["mode"] == "portable"),
        portable_replay=bool(case_input["portable_replay"]),
        native_compaction=native_compaction,
    )


def pipeline_provider(
    adapter: ModelProvider,
    *,
    idle_timeout_seconds: float | None = None,
    log_dir: Path | None = None,
) -> ModelProvider:
    """Wrap a bare adapter in the production pipeline stages, as the registry does.

    The request log is off, so only the usage line is emitted, unless ``log_dir`` names where its records go.
    """
    debug = DebugConfig() if log_dir is None else DebugConfig(log_llm_requests=True, llm_request_log_dir=str(log_dir))
    return wrap_provider(
        adapter,
        idle_timeout_seconds=idle_timeout_seconds,
        media_fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT,
        request_log=RequestLog(
            model_name="default",
            # An authored ``provider`` value, as the registry passes. The usage line reads it only to recognize
            # ``vertexai_claude``, which the adapter's own provider and Claude model id also identify in these tests.
            configured_provider="openai",
            debug=debug,
            # Records go to ``log_dir`` or nowhere, so the storage root is never read.
            storage_root=Path("unused"),
        ),
    )


async def replay_through_pipeline(
    adapter: ModelProvider,
    case_input: dict[str, Any],
    recorded: list[httpx.Request],
) -> Replay:
    """Send a golden's request through the pipeline around ``adapter`` and return what it sent and produced."""
    request = golden_request(case_input, native=adapter.native_history_route)
    if (deferred := case_input["deferred_tool_search"]) is not None:
        # The build defers natively exactly where the route has native tool search, so the recorded mode follows it.
        assert adapter.capabilities.native_tool_search == (deferred["mode"] == "native")
    events = [event async for event in pipeline_provider(adapter).stream(request)]
    if len(recorded) != 1:
        msg = f"expected one provider request, got {[str(request.url) for request in recorded]}"
        raise AssertionError(msg)
    await recorded[0].aread()
    completed = events[-1]
    assert isinstance(completed, TurnCompleted), f"the stream ended with {completed!r}"
    return Replay(request=normalized_request(recorded[0]), parsed=parsed_turn(completed), events=events)
