"""Harness for the Claude-family parity tests: load a C1 golden, send it through the owned adapter, normalize.

The goldens under ``tests/fixtures/agno_parity/providers/{anthropic,vertexai_claude,bedrock_claude}`` pin the
request Agno 3.0.9 plus MindRoom's patches sent and the message they parsed; ``README.md`` beside them
documents the format and normalization this module mirrors. Two pipeline stages that are not the adapter's
(the loop's portable tool-search projection and the media-fallback note) have minimal stand-ins here so the
request bodies compare whole.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from google.oauth2.credentials import Credentials
from pydantic import BaseModel

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, TurnCompleted
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.llm.provider import MediaKind

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"
FAMILIES = ("anthropic", "vertexai_claude", "bedrock_claude")

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
_VOLATILE_MESSAGE_KEYS = ("id", "created_at")
_TIMING_METRIC_KEYS = ("duration", "time_to_first_token")
# Provider SDKs read these environment variables directly; the harness hides them from the run.
_PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "AWS_", "CLOUD_ML_", "GOOGLE_")
# The marker and prompt the media-fallback wrapper (a sibling pipeline stage) puts on a message that lost media.
_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_CALL_TOOL_NAME = "call_tool"


def fixture_paths() -> list[Path]:
    """Return every Claude-family golden, sorted."""
    return sorted(path for family in FAMILIES for path in (FIXTURE_ROOT / family).glob("*.json"))


def fixture_id(path: Path) -> str:
    """Return the ``family/scenario`` id of one golden."""
    return f"{path.parent.name}/{path.stem}"


def _json_roundtrip(value: object) -> Any:  # noqa: ANN401
    return json.loads(json.dumps(value, sort_keys=True))


def normalized_request(request: httpx.Request) -> dict[str, Any]:
    """Return the method, URL, stable headers, and JSON body of one SDK request."""
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
    """Return the assistant message dict, its usage metrics, and the raw provider stop reason."""
    message_dict = message.to_dict()
    for key in _VOLATILE_MESSAGE_KEYS:
        message_dict.pop(key, None)
    metrics = message_dict.pop("metrics", {})
    for key in _TIMING_METRIC_KEYS:
        metrics.pop(key, None)
    provider_data = message.provider_data or {}
    return _json_roundtrip(
        {"message": message_dict, "metrics": metrics, "stop_reason": provider_data.get("stop_reason")},
    )


def expected_parsed(fixture: dict[str, Any]) -> dict[str, Any]:
    """Return the golden's parsed reply as the owned message type stores it.

    Agno's ``Message.citations`` was a non-persisted view of the citations inside ``content_blocks``; the owned
    message has no such field (``mindroom.llm.messages``), so the key is dropped from the comparison while the
    raw citations still compare inside ``provider_data.content_blocks``.
    """
    parsed = deepcopy(fixture["parsed"])
    parsed["message"].pop("citations", None)
    return parsed


def encoded_response(response_raw: dict[str, Any]) -> httpx.Response:
    """Build the provider HTTP response a golden's ``response_raw`` describes."""
    if "sse" in response_raw:
        lines: list[str] = []
        for event in response_raw["sse"]:
            if "event" in event:
                lines.append(f"event: {event['event']}")
            data = event["data"]
            lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
            lines.append("")
        content = ("\n".join(lines) + "\n").encode()
    else:
        content = json.dumps(response_raw["json"]).encode()
    return httpx.Response(response_raw["status"], headers=response_raw["headers"], content=content)


@contextmanager
def isolated_provider_environment() -> Iterator[None]:
    """Hide provider credentials and endpoints in the process environment from SDK fallbacks."""
    saved = {name: value for name, value in os.environ.items() if name.startswith(_PROVIDER_ENV_PREFIXES)}
    for name in saved:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(saved)


def provider_for(model_config: dict[str, Any], root: Path, http_client: httpx.AsyncClient) -> ClaudeProvider:
    """Build the adapter of one authored model config exactly as the loader would, over the given HTTP client.

    ``vertexai_claude`` gets the static Google credential object JSON cannot carry, as the capture did.
    """
    config = deepcopy(model_config)
    if config["provider"] == "vertexai_claude":
        extra_kwargs = config.setdefault("extra_kwargs", {})
        extra_kwargs["client_params"] = {
            **extra_kwargs.get("client_params", {}),
            "credentials": Credentials(token="parity-google-token"),  # noqa: S106 - synthetic SDK credential
        }
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={})
    provider = build_provider(ModelConfig(**config), runtime_paths)
    return replace(provider, http_client=http_client)


def _response_format(encoded: dict[str, Any] | None) -> dict[str, Any] | type[BaseModel] | None:
    if encoded is None:
        return None
    if encoded["type"] == "dict":
        return encoded["value"]
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    assert issubclass(schema, BaseModel)
    if schema.model_json_schema() != encoded["json_schema"]:
        msg = f"{encoded['model']} no longer matches the schema recorded in the fixture"
        raise AssertionError(msg)
    return schema


def _wrapped_call(call: dict[str, Any], deferred: frozenset[str]) -> dict[str, Any]:
    function = call["function"]
    if function.get("name") not in deferred:
        return call
    arguments = json.loads(function.get("arguments") or "{}")
    wrapped = json.dumps({"name": function["name"], "arguments": arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": _CALL_TOOL_NAME, "arguments": wrapped}}


def project_portable_request(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    deferred: frozenset[str],
) -> tuple[list[Message], list[ToolSpec]]:
    """Stand in for the loop's portable tool-search projection on routes without native tool search.

    Deferred schemas leave the tool list, stored calls to deferred functions replay as ``call_tool`` calls, and
    their results take the ``call_tool`` name, so the request prefix stays byte-stable.
    """
    projected: list[Message] = []
    for message in messages:
        if message.tool_calls:
            calls = [_wrapped_call(call, deferred) for call in message.tool_calls]
            projected.append(replace(message, tool_calls=calls) if calls != message.tool_calls else message)
        elif message.tool_name in deferred:
            projected.append(replace(message, tool_name=_CALL_TOOL_NAME))
        else:
            projected.append(message)
    return projected, [tool for tool in tools if tool.name not in deferred]


def without_omitted_media(messages: Sequence[Message], omitted: frozenset[MediaKind]) -> list[Message]:
    """Stand in for the media-fallback wrapper: strip the kinds the route omits and leave its note on the message."""
    note = f"{_FALLBACK_MARKER}\n{INLINE_MEDIA_FALLBACK_PROMPT}"
    stripped: list[Message] = []
    for message in messages:
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
        removed = present & omitted
        if not removed:
            stripped.append(message)
            continue
        updates: dict[str, Any] = {
            field: None
            for kind, field in (("audio", "audio"), ("image", "images"), ("file", "files"), ("video", "videos"))
            if kind in removed
        }
        content = message.content
        updates["content"] = (
            [*content, {"type": "text", "text": note}]
            if isinstance(content, list)
            else (f"{content}\n\n{note}" if content else note)
        )
        stripped.append(replace(message, **updates))
    return stripped


def request_from_input(case_input: dict[str, Any], provider: ClaudeProvider) -> ModelRequest:
    """Turn a golden's input into the owned request the loop would hand the adapter."""
    messages = [Message.from_dict(deepcopy(message)) for message in case_input["messages"]]
    tools = [
        ToolSpec(
            name=tool["name"],
            description=tool.get("description") or "",
            parameters=tool.get("parameters") or {},
            strict=bool(tool.get("strict", False)),
        )
        for tool in case_input["tools"]
    ]
    deferred_tool_names: frozenset[str] = frozenset()
    deferred = case_input["deferred_tool_search"]
    if deferred is not None:
        names = frozenset(deferred["tool_names"])
        if deferred["mode"] == "native":
            deferred_tool_names = names
        else:
            messages, tools = project_portable_request(messages, tools, names)
    messages = without_omitted_media(messages, provider.capabilities.omitted_media)
    return ModelRequest(
        messages=messages,
        tools=tools,
        tool_choice=case_input["tool_choice"],
        response_format=_response_format(case_input["response_format"]),
        deferred_tool_names=deferred_tool_names,
    )


async def run_golden(fixture: dict[str, Any], root: Path) -> dict[str, Any]:
    """Send one golden's request through the owned adapter and return the normalized request and parsed reply."""
    recorded: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return encoded_response(fixture["response_raw"])

    with isolated_provider_environment():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            provider = provider_for(fixture["input"]["model_config"], root, http_client)
            request = request_from_input(fixture["input"], provider)
            completed = [event async for event in provider.stream(request) if isinstance(event, TurnCompleted)]
    assert len(recorded) == 1, f"expected one provider request, got {[str(request.url) for request in recorded]}"
    assert len(completed) == 1
    return {"request": normalized_request(recorded[0]), "parsed": normalized_parsed(completed[0].message)}
