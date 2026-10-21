"""Replay the C1 provider goldens of the Gemini and Ollama routes through the owned adapters.

A golden pins one request as the Agno-backed provider layer sent it and the assistant message it parsed.
The harness builds the owned adapter from the golden's model config, answers its single HTTP request with
the golden's canned reply through an httpx mock transport, and returns what the adapter sent and produced.

The request projections MindRoom applies above the adapter are reproduced here the way the production
wrappers apply them: portable deferred tool search hides deferred schemas behind ``call_tool``, and the
media fallback replaces media kinds the adapter omits with the fallback note.

Owned adapters only stream, so goldens captured from Agno's non-streaming structured-output path are
replayed as a stream: Gemini's ``generateContent`` URL becomes ``streamGenerateContent?alt=sse`` and the
Ollama body says ``"stream": true``; the canned JSON reply becomes the stream's only chunk.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from google.oauth2.credentials import Credentials

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, TurnCompleted
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.gemini.adapter import build_provider as build_gemini_provider
from mindroom.providers.ollama_adapter import build_provider as build_ollama_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from pydantic import BaseModel

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import MediaKind, ModelProvider, StopReason, StreamEvent

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"
FAMILIES = ("gemini", "gemini_vertexai", "ollama")

# Request headers the SDK or transport derives from the environment, the credentials, or the body length.
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
_PROVIDER_ENV_PREFIXES = ("GEMINI_", "GOOGLE_", "OLLAMA_")
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
GENERATED_ID = "<uuid4>"
_STOP_KEYS = ("stop_reason", "finish_reason", "response_status")
_CALL_TOOL = "call_tool"
_FALLBACK_MARKER = "[Inline media unavailable for this model]"


@dataclass(frozen=True)
class Golden:
    """One C1 provider golden."""

    family: str
    scenario: str
    data: dict[str, Any]

    @property
    def id(self) -> str:
        """The pytest id."""
        return f"{self.family}/{self.scenario}"


@dataclass(frozen=True)
class Replay:
    """What the owned adapter sent and produced for one golden."""

    request: dict[str, Any]
    parsed: dict[str, Any]
    events: list[StreamEvent]
    stop_reason: StopReason | None


def goldens() -> list[Golden]:
    """Return every golden of the Gemini, Gemini on Vertex AI, and Ollama routes."""
    return [
        Golden(family=family, scenario=path.stem, data=json.loads(path.read_text(encoding="utf-8")))
        for family in FAMILIES
        for path in sorted((FIXTURE_ROOT / family).glob("*.json"))
    ]


def hide_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provider credentials and endpoints in the process environment away from SDK fallbacks."""
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)


def runtime_paths_for(root: Path) -> RuntimePaths:
    """Return an isolated runtime with an empty process environment under ``root``."""
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={})


def _scrub_generated_ids(value: object) -> Any:  # noqa: ANN401
    if isinstance(value, dict):
        return {key: _scrub_generated_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_generated_ids(item) for item in value]
    if isinstance(value, str) and _UUID4.match(value):
        return GENERATED_ID
    return value


def normalized_request(request: httpx.Request) -> dict[str, Any]:
    """Return the method, URL, stable headers, and JSON body of one SDK request, as C1 recorded them."""
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


def normalized_parsed(message: Message) -> dict[str, Any]:
    """Return the assistant message dict, its usage, and the stored stop reason, as C1 recorded them."""
    message_dict = message.to_dict()
    message_dict.pop("id")
    message_dict.pop("created_at")
    metrics = message_dict.pop("metrics", {})
    metrics.pop("duration", None)
    metrics.pop("time_to_first_token", None)
    provider_data = message.provider_data or {}
    parsed = {
        "message": message_dict,
        "metrics": metrics,
        "stop_reason": next((provider_data[key] for key in _STOP_KEYS if provider_data.get(key) is not None), None),
    }
    return _scrub_generated_ids(json.loads(json.dumps(parsed, sort_keys=True)))


def expected_request(golden: Golden) -> dict[str, Any]:
    """Return the golden request as the stream-only adapter sends it."""
    expected = copy.deepcopy(golden.data["request"])
    if golden.data["input"]["stream"]:
        return expected
    if golden.family == "ollama":
        expected["body"]["stream"] = True
    else:
        expected["url"] = expected["url"].replace(":generateContent", ":streamGenerateContent?alt=sse")
    return expected


def _encoded_response(golden: Golden) -> httpx.Response:
    raw = golden.data["response_raw"]
    if "sse" in raw:
        events = [event["data"] for event in raw["sse"]]
    elif "ndjson" in raw:
        events = raw["ndjson"]
    else:
        events = [raw["json"]]
    if golden.family == "ollama":
        content = b"".join(json.dumps(event).encode() + b"\n" for event in events)
        content_type = "application/x-ndjson"
    else:
        content = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        content_type = "text/event-stream"
    return httpx.Response(raw["status"], headers={"content-type": content_type}, content=content)


def build_adapter(
    model_config: dict[str, Any],
    root: Path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> ModelProvider:
    """Build the owned adapter for a golden model config with its HTTP traffic sent to ``handler``."""
    config = copy.deepcopy(model_config)
    extra_kwargs = config.setdefault("extra_kwargs", {})
    transport = httpx.MockTransport(handler)
    if config["provider"] == "ollama":
        extra_kwargs["client_params"] = {"transport": transport}
        return build_ollama_provider(ModelConfig(**config), runtime_paths_for(root))
    extra_kwargs["client_params"] = {"http_options": {"httpx_async_client": httpx.AsyncClient(transport=transport)}}
    if extra_kwargs.get("vertexai"):
        extra_kwargs["credentials"] = Credentials(token="parity-google-token")  # noqa: S106 - synthetic credential
    return build_gemini_provider(ModelConfig(**config), runtime_paths_for(root))


def _response_format(encoded: dict[str, Any] | None) -> type[BaseModel] | dict[str, Any] | None:
    if encoded is None:
        return None
    if encoded["type"] == "dict":
        return encoded["value"]
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    if schema.model_json_schema() != encoded["json_schema"]:
        msg = f"{encoded['model']} no longer matches the schema recorded in the golden"
        raise AssertionError(msg)
    return schema


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call["function"]
    if function["name"] not in deferred:
        return call
    arguments = json.loads(function["arguments"])
    wrapped = json.dumps({"name": function["name"], "arguments": arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": _CALL_TOOL, "arguments": wrapped}}


def _portable_projection(message: Message, deferred: frozenset[str]) -> Message:
    """Present a stored deferred call and its result as ``call_tool``, as portable tool search does."""
    if message.tool_calls:
        return replace(message, tool_calls=[_wrapped_call(call, deferred) for call in message.tool_calls])
    if message.tool_name in deferred:
        return replace(message, tool_name=_CALL_TOOL)
    return message


def _without_omitted_media(message: Message, omitted: frozenset[MediaKind]) -> Message:
    """Replace media the adapter omits with the fallback note, as the media fallback does."""
    present = {
        kind
        for kind, media in (
            ("audio", message.audio),
            ("image", message.images),
            ("file", message.files),
            ("video", message.videos),
        )
        if media
    }
    if not present & omitted:
        return message
    note = f"{_FALLBACK_MARKER}\n{INLINE_MEDIA_FALLBACK_PROMPT}"
    return replace(
        message,
        content=f"{message.content}\n\n{note}" if message.content else note,
        audio=None if "audio" in omitted else message.audio,
        images=None if "image" in omitted else message.images,
        files=None if "file" in omitted else message.files,
        videos=None if "video" in omitted else message.videos,
    )


def model_request(golden: Golden, adapter: ModelProvider) -> ModelRequest:
    """Return the request the run loop hands the adapter for one golden."""
    case_input = golden.data["input"]
    messages = [Message.from_dict(copy.deepcopy(message)) for message in case_input["messages"]]
    tools = [
        ToolSpec(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=tool.get("parameters", {}),
            strict=tool.get("strict", False),
        )
        for tool in case_input["tools"]
    ]
    if (deferred_search := case_input["deferred_tool_search"]) is not None:
        deferred = frozenset(deferred_search["tool_names"])
        tools = [tool for tool in tools if tool.name not in deferred]
        messages = [_portable_projection(message, deferred) for message in messages]
    omitted = adapter.capabilities.omitted_media
    return ModelRequest(
        messages=[_without_omitted_media(message, omitted) for message in messages],
        tools=tools,
        tool_choice=case_input["tool_choice"],
        response_format=_response_format(case_input["response_format"]),
    )


async def replay(golden: Golden, root: Path) -> Replay:
    """Send one golden's request through the owned adapter and return what it sent and produced."""
    recorded: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return _encoded_response(golden)

    adapter = build_adapter(golden.data["input"]["model_config"], root, respond)
    events = [event async for event in adapter.stream(model_request(golden, adapter))]
    if len(recorded) != 1:
        msg = f"expected one provider request, got {[str(request.url) for request in recorded]}"
        raise AssertionError(msg)
    completed = events[-1]
    assert isinstance(completed, TurnCompleted), f"the stream ended with {completed!r}"
    return Replay(
        request=normalized_request(recorded[0]),
        parsed=normalized_parsed(completed.message),
        events=events,
        stop_reason=completed.stop_reason,
    )


def gemini_sse(*chunks: dict[str, Any], status: int = 200) -> httpx.Response:
    """Return a streamed Gemini reply made of ``chunks``."""
    content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=content)


def ollama_ndjson(*chunks: dict[str, Any], status: int = 200) -> httpx.Response:
    """Return a streamed Ollama reply made of ``chunks``."""
    content = b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)
    return httpx.Response(status, headers={"content-type": "application/x-ndjson"}, content=content)


async def run_adapter(
    model_config: dict[str, Any],
    request: ModelRequest,
    root: Path,
    response: httpx.Response | Exception,
) -> tuple[dict[str, Any], list[StreamEvent]]:
    """Send ``request`` through the adapter for ``model_config`` and return its HTTP request and events."""
    recorded: list[httpx.Request] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        recorded.append(http_request)
        if isinstance(response, Exception):
            raise response
        return response

    adapter = build_adapter(model_config, root, respond)
    events = [event async for event in adapter.stream(request)]
    return normalized_request(recorded[0]), events
