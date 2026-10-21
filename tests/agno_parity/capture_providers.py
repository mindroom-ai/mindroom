"""Capture provider wire goldens from the Agno 3.0.9 provider layer.

Each golden pins one provider request and its parsed reply as MindRoom sends and reads them today.
The model comes from ``get_model_instance`` with every hook production installs, the global
run-message patches are active, and the request goes through Agno's ``aresponse_stream`` (or
``aresponse`` for structured output) entry point the run loop uses, so every MindRoom
provider patch shapes the request and the parse.
A transport-level stub answers the single provider request with the canned ``response_raw``.

Regenerate after a deliberate change with ``.venv/bin/python -m tests.agno_parity.capture_providers``.
``tests/fixtures/agno_parity/README.md`` documents the fixture format and normalization.
Task B8 deletes this module together with Agno; the fixtures stay.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import tempfile
from contextlib import aclosing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
from agno.agent import Agent
from agno.agent._response import get_response_format
from agno.models.message import Message
from agno.models.response import ModelResponse, ModelResponseEvent
from agno.run.base import RunContext
from agno.tools.function import Function
from google.genai import _api_client as genai_api_client
from google.oauth2.credentials import Credentials

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.claude_prompt_cache import aclose_anthropic_async_client, install_claude_deferred_tool_search
from mindroom.providers.model_http_client import aclose_model_http_client
from mindroom.providers.model_loading import get_model_instance
from mindroom.providers.openai_tool_search import install_openai_deferred_tool_search
from mindroom.providers.portable_tool_search import install_portable_deferred_tool_search, resolve_call_tool_calls
from mindroom.routing import _RoutingSuggestion
from mindroom.session_storage import agno_compat_run_messages
from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.models.base import Model
    from pydantic import BaseModel

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"

# ---------------------------------------------------------------------------
# Normalization shared by capture and replay
# ---------------------------------------------------------------------------

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
# Message keys whose values change per run, and metric keys measured from the wall clock.
_VOLATILE_MESSAGE_KEYS = ("id", "created_at")
_TIMING_METRIC_KEYS = ("duration", "time_to_first_token")
_STOP_KEYS = ("stop_reason", "finish_reason", "response_status")
# The Cerebras SDK warms its connection with this request when a client is built; it is not a model request.
_CEREBRAS_WARMING_PATH = "/v1/tcp_warming"

# Placeholders in fixture model configs that the harness replaces with local credential state.
CODEX_HOME = "<codex_home>"
KIMI_HOME = "<kimi_home>"
# Far-future expiry so borrowed OAuth tokens never refresh: 2100-01-01T00:00:00Z.
_TOKEN_EXPIRY = 4102444800
# Provider SDKs and Agno read these environment variables directly; the harness clears them.
_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CEREBRAS_",
    "CLOUD_ML_",
    "CODEX_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "GROQ_",
    "KIMI_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
    "ZAI_",
)


def _json_roundtrip(value: object) -> Any:  # noqa: ANN401
    """Return the value as the key-sorted JSON a fixture file stores, which is also what replays read."""
    return json.loads(json.dumps(value, sort_keys=True))


def _scrub_generated_ids(value: object) -> Any:  # noqa: ANN401
    """Replace uuid4 values Agno generates for provider-less ids with a placeholder."""
    if isinstance(value, dict):
        return {key: _scrub_generated_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_generated_ids(item) for item in value]
    if isinstance(value, str) and _UUID4.match(value):
        return GENERATED_ID
    return value


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
    """Return the assistant message dict, its usage metrics, and the provider stop reason."""
    message_dict = message.to_dict()
    for key in _VOLATILE_MESSAGE_KEYS:
        message_dict.pop(key, None)
    metrics = message_dict.pop("metrics", {})
    for key in _TIMING_METRIC_KEYS:
        metrics.pop(key, None)
    if message.citations is not None:
        message_dict["citations"] = message.citations.model_dump(mode="json", exclude_none=True)
    provider_data = message.provider_data or {}
    parsed = {
        "message": message_dict,
        "metrics": metrics,
        "stop_reason": next((provider_data[key] for key in _STOP_KEYS if provider_data.get(key) is not None), None),
    }
    return _scrub_generated_ids(_json_roundtrip(parsed))


# ---------------------------------------------------------------------------
# Transport stub
# ---------------------------------------------------------------------------


def encoded_response(response_raw: dict[str, Any]) -> httpx.Response:
    """Build the provider HTTP response a fixture's ``response_raw`` describes."""
    if "sse" in response_raw:
        lines: list[str] = []
        for event in response_raw["sse"]:
            if "event" in event:
                lines.append(f"event: {event['event']}")
            data = event["data"]
            lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
            lines.append("")
        content = ("\n".join(lines) + "\n").encode()
    elif "ndjson" in response_raw:
        content = b"".join(json.dumps(line).encode() + b"\n" for line in response_raw["ndjson"])
    else:
        content = json.dumps(response_raw["json"]).encode()
    return httpx.Response(response_raw["status"], headers=response_raw["headers"], content=content)


@contextmanager
def stubbed_provider_wire(response_raw: dict[str, Any]) -> Iterator[list[httpx.Request]]:
    """Answer every HTTP request any SDK sends with ``response_raw`` and record the requests.

    Patching the httpx transports reaches every client production builds (MindRoom's pooled
    client, Agno's shared client, and SDK-built clients) without changing how models build them.
    google-genai prefers aiohttp when it is installed, so the harness turns that off for the call.
    """
    recorded: list[httpx.Request] = []
    async_handle = httpx.AsyncHTTPTransport.handle_async_request
    sync_handle = httpx.HTTPTransport.handle_request
    use_aiohttp = genai_api_client.has_aiohttp

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(_CEREBRAS_WARMING_PATH):
            return httpx.Response(200, json={})
        recorded.append(request)
        return encoded_response(response_raw)

    async def handle_async(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        await request.aread()
        return respond(request)

    def handle_sync(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        request.read()
        return respond(request)

    httpx.AsyncHTTPTransport.handle_async_request = handle_async
    httpx.HTTPTransport.handle_request = handle_sync
    genai_api_client.has_aiohttp = False
    try:
        yield recorded
    finally:
        httpx.AsyncHTTPTransport.handle_async_request = async_handle
        httpx.HTTPTransport.handle_request = sync_handle
        genai_api_client.has_aiohttp = use_aiohttp


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


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


def _unsigned_jwt(claims: dict[str, Any]) -> str:
    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.parity"


def _write_local_credentials(root: Path) -> dict[str, str]:
    """Write the Codex and Kimi CLI credential files that fixture placeholders point at."""
    codex_home = root / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _unsigned_jwt({"exp": _TOKEN_EXPIRY}),
                    "refresh_token": "parity-refresh-token",
                    "account_id": "parity-account",
                },
            },
        ),
        encoding="utf-8",
    )
    (codex_home / "installation_id").write_text("parity-installation\n", encoding="utf-8")
    kimi_home = root / "kimi"
    (kimi_home / "credentials").mkdir(parents=True)
    (kimi_home / "credentials" / "kimi-code.json").write_text(
        json.dumps(
            {"access_token": "parity-kimi-token", "refresh_token": "parity-refresh-token", "expires_at": _TOKEN_EXPIRY},
        ),
        encoding="utf-8",
    )
    return {CODEX_HOME: str(codex_home), KIMI_HOME: str(kimi_home)}


def _runtime_model_config(model_config: dict[str, Any], root: Path) -> ModelConfig:
    """Resolve placeholders and add the Google credential objects JSON cannot carry."""
    placeholders = _write_local_credentials(root)
    config = deepcopy(model_config)
    extra_kwargs = config.setdefault("extra_kwargs", {})
    for key, value in list(extra_kwargs.items()):
        if isinstance(value, str) and value in placeholders:
            extra_kwargs[key] = placeholders[value]
    token = Credentials(token="parity-google-token")  # noqa: S106 - synthetic SDK credential
    if config["provider"] == "vertexai_claude":
        extra_kwargs["client_params"] = {**extra_kwargs.get("client_params", {}), "credentials": token}
    if extra_kwargs.get("vertexai"):
        extra_kwargs["credentials"] = token
    return ModelConfig(**config)


def build_model(case_input: dict[str, Any], root: Path) -> Model:
    """Build the fixture's model exactly as an agent turn does."""
    agno_compat_run_messages.install_patch()
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={})
    config = Config.validate_with_runtime(
        Config(models={"default": _runtime_model_config(case_input["model_config"], root)}).authored_model_dump(),
        runtime_paths,
    )
    model = get_model_instance(config, runtime_paths, "default")
    deferred = case_input["deferred_tool_search"]
    if deferred is not None:
        names = frozenset(deferred["tool_names"])
        if deferred["mode"] == "portable":
            install_portable_deferred_tool_search(model, deferred_tool_names=names)
        else:
            install_claude_deferred_tool_search(model, deferred_tool_names=names)
            install_openai_deferred_tool_search(model, deferred_tool_names=names)
    if case_input["portable_replay"] is not None:
        model.configure_portable_replay(enabled=case_input["portable_replay"])
    return model


def _response_format_from_input(encoded: dict[str, Any] | None) -> dict[str, Any] | type[BaseModel] | None:
    if encoded is None:
        return None
    if encoded["type"] == "dict":
        return encoded["value"]
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    if schema.model_json_schema() != encoded["json_schema"]:
        msg = f"{encoded['model']} no longer matches the schema recorded in the fixture"
        raise AssertionError(msg)
    return schema


def _encoded_response_format(value: dict[str, Any] | type[BaseModel] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return {"type": "dict", "value": value}
    return {
        "type": "pydantic",
        "model": f"{value.__module__}:{value.__qualname__}",
        "json_schema": value.model_json_schema(),
    }


# ---------------------------------------------------------------------------
# Running one golden
# ---------------------------------------------------------------------------


async def _run(case_input: dict[str, Any], response_raw: dict[str, Any], root: Path) -> dict[str, Any]:
    model = build_model(case_input, root)
    messages = [Message.from_dict(deepcopy(message)) for message in case_input["messages"]]
    tools = [Function.from_dict(tool) for tool in case_input["tools"]] or None
    response_format = _response_format_from_input(case_input["response_format"])
    try:
        with stubbed_provider_wire(response_raw) as recorded:
            if case_input["stream"]:
                # Stop after the first provider request; tool execution is the run loop's concern.
                async with aclosing(
                    model.aresponse_stream(
                        messages=messages,
                        response_format=response_format,
                        tools=tools,
                        tool_choice=case_input["tool_choice"],
                    ),
                ) as stream:
                    async for event in stream:
                        if (
                            isinstance(event, ModelResponse)
                            and event.event == ModelResponseEvent.model_request_completed.value
                        ):
                            break
            else:
                # Agno drops the adapter's parsed output here; agents parse the reply text instead.
                await model.aresponse(
                    messages=messages,
                    response_format=response_format,
                    tools=tools,
                    tool_choice=case_input["tool_choice"],
                )
    finally:
        await aclose_anthropic_async_client(model)
        await aclose_model_http_client()
    if len(recorded) != 1:
        msg = f"expected one provider request, got {[str(request.url) for request in recorded]}"
        raise AssertionError(msg)
    return {"request": normalized_request(recorded[0]), "parsed": normalized_parsed(messages[-1])}


def run_golden(case_input: dict[str, Any], response_raw: dict[str, Any]) -> dict[str, Any]:
    """Send one fixture's request through the current provider layer and return what it produced."""
    with (
        isolated_provider_environment(),
        tempfile.TemporaryDirectory(prefix="agno-parity-") as root,
    ):
        return asyncio.run(_run(case_input, response_raw, Path(root)))


def resolved_response_format(case_input: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any] | None:
    """Resolve an output schema to the response format Agno's agent passes this model."""
    with (
        isolated_provider_environment(),
        tempfile.TemporaryDirectory(prefix="agno-parity-") as root,
    ):
        model = build_model(case_input, Path(root))
        agent = Agent(model=model, output_schema=schema, telemetry=False)
        run_context = RunContext(run_id="parity-run", session_id="parity-session", output_schema=schema)
        return _encoded_response_format(get_response_format(agent, model=model, run_context=run_context))


# ---------------------------------------------------------------------------
# Canned provider replies
# ---------------------------------------------------------------------------

CREATED = 1792540800  # 2026-10-20T00:00:00Z
CREATED_ISO = "2026-10-20T00:00:00.000000Z"


@dataclass(frozen=True)
class Call:
    """One tool call a canned reply makes, with ids in each wire family's native format.

    ``key=None`` mimics providers that omit call ids.
    """

    key: str | None
    name: str
    arguments: dict[str, Any]

    def native_id(self, prefix: str) -> str | None:
        """Return the call id a provider using ``prefix`` would send."""
        return None if self.key is None else f"{prefix}parity_{self.key}"


@dataclass(frozen=True)
class Reply:
    """A provider-neutral description of one canned reply, encoded per wire family."""

    text: str = ""
    reasoning: str | None = None
    calls: tuple[Call, ...] = ()
    input_tokens: int = 42
    output_tokens: int = 12
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # Claude content blocks emitted before the reply's text and tool_use blocks.
    claude_blocks: tuple[dict[str, Any], ...] = ()
    # Claude citations attached to the reply's text block.
    claude_citations: tuple[dict[str, Any], ...] = ()
    # Responses output items emitted before the reply's message and function calls.
    responses_items: tuple[dict[str, Any], ...] = ()


def _pieces(text: str) -> list[str]:
    """Split text into two stream deltas at a word boundary."""
    cut = text.find(" ", len(text) // 2)
    return [text] if cut < 0 else [text[:cut], text[cut:]]


def _sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": 200, "headers": {"content-type": "text/event-stream"}, "sse": events}


def _json_body(body: dict[str, Any]) -> dict[str, Any]:
    return {"status": 200, "headers": {"content-type": "application/json"}, "json": body}


def _chat_usage(reply: Reply, family: Family) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": reply.input_tokens,
        "completion_tokens": reply.output_tokens,
        "total_tokens": reply.input_tokens + reply.output_tokens,
        "prompt_tokens_details": {"cached_tokens": reply.cached_tokens},
        "completion_tokens_details": {"reasoning_tokens": reply.reasoning_tokens},
    }
    if family.usage_style == "openrouter":
        usage["cost"] = 0.00021
    if family.usage_style == "groq":
        usage |= {"queue_time": 0.011, "prompt_time": 0.004, "completion_time": 0.021, "total_time": 0.025}
    return usage


_CEREBRAS_TIME_INFO = {"queue_time": 0.001, "prompt_time": 0.002, "completion_time": 0.01, "total_time": 0.013}


def _chat_tool_call_deltas(reply: Reply, family: Family) -> list[dict[str, Any]]:
    """Return tool-call deltas: a named start, then the arguments, or the whole call on Groq and Cerebras."""
    deltas = []
    for index, call in enumerate(reply.calls):
        arguments = json.dumps(call.arguments)
        start: dict[str, Any] = {"index": index, "type": "function", "function": {"name": call.name, "arguments": ""}}
        if (call_id := call.native_id("call_")) is not None:
            start["id"] = call_id
        if family.usage_style in {"groq", "cerebras"}:
            deltas.append({"tool_calls": [{**start, "function": {"name": call.name, "arguments": arguments}}]})
        else:
            deltas.append({"tool_calls": [start]})
            deltas.append({"tool_calls": [{"index": index, "function": {"arguments": arguments}}]})
    return deltas


def _chat_reply(reply: Reply, family: Family, *, stream: bool) -> dict[str, Any]:
    usage = _chat_usage(reply, family)
    finish_reason = "tool_calls" if reply.calls else "stop"
    base = {
        "id": "chatcmpl-parity",
        "object": "chat.completion.chunk" if stream else "chat.completion",
        "created": CREATED,
        "model": family.wire_model_id,
        "system_fingerprint": "fp_parity",
    }
    if not stream:
        message = {"role": "assistant", "content": reply.text}
        body = {**base, "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}], "usage": usage}
        if family.usage_style == "cerebras":
            body["time_info"] = _CEREBRAS_TIME_INFO
        return _json_body(body)

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    deltas: list[dict[str, Any]] = [{"role": "assistant", "content": ""}]
    if reply.reasoning:
        reasoning: dict[str, Any] = {family.chat_reasoning_field: reply.reasoning}
        if family.usage_style == "openrouter":
            reasoning["reasoning_details"] = [
                {"type": "reasoning.text", "text": reply.reasoning, "format": "unknown", "index": 0},
            ]
        deltas.append(reasoning)
    deltas.extend({"content": piece} for piece in _pieces(reply.text) if reply.text)
    deltas.extend(_chat_tool_call_deltas(reply, family))
    chunks = [chunk(delta) for delta in deltas]
    final = chunk({}, finish_reason)
    # OpenAI-compatible servers send usage in a trailing chunk; Groq and Cerebras put it on the final chunk.
    if family.usage_style == "groq":
        chunks.append({**final, "x_groq": {"id": "req_parity", "usage": usage}})
    elif family.usage_style == "cerebras":
        chunks.append({**final, "usage": usage, "time_info": _CEREBRAS_TIME_INFO})
    else:
        chunks.extend([final, {**base, "choices": [], "usage": usage}])
    return _sse([*({"data": item} for item in chunks), {"data": "[DONE]"}])


def _claude_blocks(reply: Reply) -> list[dict[str, Any]]:
    blocks = [dict(block) for block in reply.claude_blocks]
    if reply.reasoning:
        blocks.insert(
            0,
            {"type": "thinking", "thinking": reply.reasoning, "signature": "parity-thinking-signature"},
        )
    if reply.text:
        text_block: dict[str, Any] = {"type": "text", "text": reply.text}
        if reply.claude_citations:
            text_block["citations"] = list(reply.claude_citations)
        blocks.append(text_block)
    blocks.extend(
        {"type": "tool_use", "id": call.native_id("toolu_"), "name": call.name, "input": call.arguments}
        for call in reply.calls
    )
    return blocks


def _claude_usage(reply: Reply, *, output_tokens: int) -> dict[str, Any]:
    return {
        "input_tokens": reply.input_tokens,
        "cache_creation_input_tokens": reply.cache_write_tokens,
        "cache_read_input_tokens": reply.cached_tokens,
        "output_tokens": output_tokens,
    }


def _claude_reply(reply: Reply, family: Family, *, stream: bool) -> dict[str, Any]:
    blocks = _claude_blocks(reply)
    stop_reason = "tool_use" if reply.calls else "end_turn"
    message = {
        "id": "msg_parity",
        "type": "message",
        "role": "assistant",
        "model": family.wire_model_id,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _claude_usage(reply, output_tokens=reply.output_tokens),
    }
    if not stream:
        return _json_body(message)
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                **message,
                "content": [],
                "stop_reason": None,
                "usage": _claude_usage(reply, output_tokens=1),
            },
        },
    ]
    for index, block in enumerate(blocks):
        kind = block["type"]
        deltas: list[dict[str, Any]] = []
        if kind == "text":
            start: dict[str, Any] = {"type": "text", "text": ""}
            if "citations" in block:
                start["citations"] = []
                deltas.extend({"type": "citations_delta", "citation": citation} for citation in block["citations"])
            deltas.extend({"type": "text_delta", "text": piece} for piece in _pieces(block["text"]))
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif kind in {"tool_use", "server_tool_use"}:
            start = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        else:
            start = block
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": reply.output_tokens},
        },
    )
    events.append({"type": "message_stop"})
    return _sse([{"event": event["type"], "data": event} for event in events])


def _responses_items(reply: Reply) -> list[dict[str, Any]]:
    items = [dict(item) for item in reply.responses_items]
    if reply.reasoning:
        items.insert(
            0,
            {
                "id": "rs_parity",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reply.reasoning}],
                "encrypted_content": "parity-encrypted-reasoning",
            },
        )
    if reply.text:
        items.append(
            {
                "id": "msg_parity",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": reply.text, "annotations": []}],
            },
        )
    items.extend(
        {
            "id": call.native_id("fc_"),
            "type": "function_call",
            "status": "completed",
            "call_id": call.native_id("call_"),
            "name": call.name,
            "arguments": json.dumps(call.arguments),
        }
        for call in reply.calls
    )
    return items


def _responses_object(reply: Reply, family: Family, *, status: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_parity",
        "object": "response",
        "created_at": CREATED,
        "status": status,
        "model": family.wire_model_id,
        "output": items,
        "error": None,
        "incomplete_details": None,
        "usage": None
        if status != "completed"
        else {
            "input_tokens": reply.input_tokens,
            "input_tokens_details": {"cached_tokens": reply.cached_tokens},
            "output_tokens": reply.output_tokens,
            "output_tokens_details": {"reasoning_tokens": reply.reasoning_tokens},
            "total_tokens": reply.input_tokens + reply.output_tokens,
        },
    }


def _responses_item_events(index: int, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the stream events between one output item's added and done events."""
    kind = item["type"]
    if kind == "message":
        text = item["content"][0]["text"]
        part = {"item_id": item["id"], "output_index": index, "content_index": 0}
        return [
            {
                "type": "response.content_part.added",
                **part,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            *(
                {"type": "response.output_text.delta", **part, "delta": piece, "logprobs": []}
                for piece in _pieces(text)
            ),
            {"type": "response.output_text.done", **part, "text": text, "logprobs": []},
            {"type": "response.content_part.done", **part, "part": item["content"][0]},
        ]
    if kind == "reasoning":
        text = item["summary"][0]["text"]
        part = {"item_id": item["id"], "output_index": index, "summary_index": 0}
        return [
            {"type": "response.reasoning_summary_part.added", **part, "part": {"type": "summary_text", "text": ""}},
            {"type": "response.reasoning_summary_text.delta", **part, "delta": text},
            {"type": "response.reasoning_summary_text.done", **part, "text": text},
            {"type": "response.reasoning_summary_part.done", **part, "part": item["summary"][0]},
        ]
    if kind == "function_call":
        part = {"item_id": item["id"], "output_index": index}
        return [
            {"type": "response.function_call_arguments.delta", **part, "delta": item["arguments"]},
            {"type": "response.function_call_arguments.done", **part, "arguments": item["arguments"]},
        ]
    return []


def _responses_added_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["type"] == "message":
        return {**item, "status": "in_progress", "content": []}
    if item["type"] == "reasoning":
        return {key: value for key, value in item.items() if key != "encrypted_content"} | {"summary": []}
    if item["type"] == "function_call":
        return {**item, "status": "in_progress", "arguments": ""}
    return item


def _responses_reply(reply: Reply, family: Family, *, stream: bool) -> dict[str, Any]:
    items = _responses_items(reply)
    if not stream and not family.always_streams:
        return _json_body(_responses_object(reply, family, status="completed", items=items))
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": _responses_object(reply, family, status="in_progress", items=[])},
    ]
    for index, item in enumerate(items):
        events.append(
            {"type": "response.output_item.added", "output_index": index, "item": _responses_added_item(item)},
        )
        events.extend(_responses_item_events(index, item))
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append(
        {"type": "response.completed", "response": _responses_object(reply, family, status="completed", items=items)},
    )
    return _sse(
        [{"event": event["type"], "data": {**event, "sequence_number": number}} for number, event in enumerate(events)],
    )


def _gemini_parts(reply: Reply) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if reply.reasoning:
        parts.append({"text": reply.reasoning, "thought": True})
    if reply.text:
        parts.extend({"text": piece} for piece in _pieces(reply.text))
    for call in reply.calls:
        function_call: dict[str, Any] = {"name": call.name, "args": call.arguments}
        if (call_id := call.native_id("")) is not None:
            function_call["id"] = call_id
        parts.append({"functionCall": function_call})
    # Gemini signs the first part after its thoughts, or the first function call.
    signed = next((part for part in parts if "functionCall" in part), None) or (parts[-1] if reply.reasoning else None)
    if signed is not None:
        signed["thoughtSignature"] = base64.b64encode(b"parity-thought-signature").decode()
    return parts


def _gemini_usage(reply: Reply, *, final: bool) -> dict[str, Any]:
    output_tokens = reply.output_tokens if final else 1
    usage: dict[str, Any] = {
        "promptTokenCount": reply.input_tokens,
        "candidatesTokenCount": output_tokens,
        "totalTokenCount": reply.input_tokens + output_tokens + reply.reasoning_tokens,
    }
    if reply.reasoning_tokens:
        usage["thoughtsTokenCount"] = reply.reasoning_tokens
    if reply.cached_tokens:
        usage["cachedContentTokenCount"] = reply.cached_tokens
    return usage


def _gemini_reply(reply: Reply, family: Family, *, stream: bool) -> dict[str, Any]:
    parts = _gemini_parts(reply)
    if not stream:
        candidate = {"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}
        return _json_body(
            {
                "candidates": [candidate],
                "usageMetadata": _gemini_usage(reply, final=True),
                "modelVersion": family.wire_model_id,
                "responseId": "parity-response",
            },
        )
    chunks = []
    for index, part in enumerate(parts):
        final = index == len(parts) - 1
        candidate: dict[str, Any] = {"content": {"role": "model", "parts": [part]}, "index": 0}
        if final:
            candidate["finishReason"] = "STOP"
        chunks.append(
            {
                "candidates": [candidate],
                "usageMetadata": _gemini_usage(reply, final=final),
                "modelVersion": family.wire_model_id,
                "responseId": "parity-response",
            },
        )
    return _sse([{"data": item} for item in chunks])


def _ollama_reply(reply: Reply, family: Family, *, stream: bool) -> dict[str, Any]:
    base = {"model": family.wire_model_id, "created_at": CREATED_ISO}
    final = {
        **base,
        "message": {"role": "assistant", "content": "" if stream else reply.text},
        "done": True,
        "done_reason": "stop",
        "total_duration": 812000000,
        "load_duration": 21000000,
        "prompt_eval_count": reply.input_tokens,
        "prompt_eval_duration": 120000000,
        "eval_count": reply.output_tokens,
        "eval_duration": 640000000,
    }
    if not stream:
        return _json_body(final)
    lines = [
        {**base, "message": {"role": "assistant", "content": piece}, "done": False} for piece in _pieces(reply.text)
    ]
    if reply.calls:
        tool_calls = [{"function": {"name": call.name, "arguments": call.arguments}} for call in reply.calls]
        lines.append({**base, "message": {"role": "assistant", "content": "", "tool_calls": tool_calls}, "done": False})
    lines.append(final)
    return {"status": 200, "headers": {"content-type": "application/x-ndjson"}, "ndjson": lines}


_ENCODERS: dict[str, Callable[..., dict[str, Any]]] = {
    "chat": _chat_reply,
    "claude": _claude_reply,
    "gemini": _gemini_reply,
    "ollama": _ollama_reply,
    "responses": _responses_reply,
}

# ---------------------------------------------------------------------------
# Provider families
# ---------------------------------------------------------------------------

_KEY = "parity-test-key"
_MEDIA = frozenset({"image", "file", "audio"})


@dataclass(frozen=True)
class Family:
    """One provider configuration and the scenario variants it supports."""

    name: str
    wire: Literal["chat", "claude", "gemini", "ollama", "responses"]
    model_config: dict[str, Any]
    deferred_mode: Literal["native", "portable"] = "portable"
    usage_style: Literal["openai", "openrouter", "groq", "cerebras"] = "openai"
    chat_reasoning_field: str = "reasoning_content"
    # extra_kwargs that enable reasoning output; None means the adapter parses no streamed reasoning.
    reasoning_kwargs: dict[str, Any] | None = None
    # model_config overrides for the prompt-cache scenario; None means no prompt-cache setting applies.
    prompt_cache_config: dict[str, Any] | None = None
    always_streams: bool = False
    # Family-specific goldens, each listed after the golden that seeds its history.
    extra_scenarios: tuple[str, ...] = ()

    @property
    def wire_model_id(self) -> str:
        """Return the model id the provider reports back."""
        return str(self.model_config["id"]).removeprefix("openai-codex/")


_CLAUDE_REASONING = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
_CLAUDE_PROMPT_CACHE_OFF = {"extra_kwargs": {"cache_system_prompt": False, "extended_cache_time": False}}
_CLAUDE_EXTRAS = ("claude_citations", "claude_citations_replay", "prompt_cache_disabled")
_NATIVE_CLAUDE_EXTRAS = (*_CLAUDE_EXTRAS, "claude_server_tool_blocks", "claude_server_tool_blocks_replay")
_GEMINI_REASONING = {"thinking_budget": 1024, "include_thoughts": True}

FAMILIES: tuple[Family, ...] = (
    Family(
        "openai_chat",
        "chat",
        {"provider": "openai", "id": "gpt-6-astra", "api": "chat_completions", "api_key": _KEY},
    ),
    Family(
        "openai_responses",
        "responses",
        {"provider": "openai", "id": "gpt-6-astra", "api": "responses", "api_key": _KEY},
        deferred_mode="native",
        reasoning_kwargs={"reasoning_effort": "medium"},
        prompt_cache_config={},
        extra_scenarios=(
            "responses_continuation",
            "reasoning_tool_call",
            "responses_explicit_replay",
            "responses_store_false_turn",
            "responses_store_false",
        ),
    ),
    Family(
        "codex",
        "responses",
        {"provider": "codex", "id": "gpt-6.1-sol", "extra_kwargs": {"codex_home": CODEX_HOME}},
        deferred_mode="native",
        reasoning_kwargs={"reasoning_effort": "medium"},
        prompt_cache_config={
            "extra_kwargs": {"prompt_cache_key": "parity-cache-key", "session_id": "parity-session"},
        },
        always_streams=True,
        extra_scenarios=("reasoning_tool_call", "responses_explicit_replay"),
    ),
    Family(
        "azure",
        "chat",
        {
            "provider": "azure",
            "id": "gpt-6-astra",
            "api_key": _KEY,
            "extra_kwargs": {
                "azure_endpoint": "https://parity-resource.openai.azure.com",
                "azure_deployment": "parity-deployment",
            },
        },
    ),
    Family(
        "openrouter",
        "chat",
        {"provider": "openrouter", "id": "z-ai/glm-5.3", "api_key": _KEY},
        usage_style="openrouter",
        chat_reasoning_field="reasoning",
        reasoning_kwargs={},
        prompt_cache_config={
            "id": "anthropic/claude-sonnet-5.5",
            "extra_kwargs": {"cache_system_prompt": True, "extended_cache_time": True},
        },
    ),
    Family(
        "deepseek",
        "chat",
        {"provider": "deepseek", "id": "deepseek-flash", "api_key": _KEY},
        reasoning_kwargs={},
    ),
    Family("zai", "chat", {"provider": "zai", "id": "glm-5.3", "api_key": _KEY}, reasoning_kwargs={}),
    Family(
        "llama_cpp",
        "chat",
        {"provider": "llama_cpp", "id": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"},
        reasoning_kwargs={},
    ),
    Family(
        "kimi",
        "chat",
        {"provider": "kimi", "id": "k3", "extra_kwargs": {"kimi_home": KIMI_HOME}},
        reasoning_kwargs={},
        prompt_cache_config={"extra_kwargs": {"prompt_cache_key": "parity-cache-key"}},
    ),
    Family(
        "anthropic",
        "claude",
        {"provider": "anthropic", "id": "claude-sonnet-5-5", "api_key": _KEY},
        deferred_mode="native",
        reasoning_kwargs=_CLAUDE_REASONING,
        prompt_cache_config={},
        extra_scenarios=_NATIVE_CLAUDE_EXTRAS,
    ),
    Family(
        "vertexai_claude",
        "claude",
        {
            "provider": "vertexai_claude",
            "id": "claude-sonnet-5-5",
            "extra_kwargs": {"project_id": "parity-project", "region": "us-east5"},
        },
        deferred_mode="native",
        reasoning_kwargs=_CLAUDE_REASONING,
        prompt_cache_config={},
        extra_scenarios=_NATIVE_CLAUDE_EXTRAS,
    ),
    Family(
        "bedrock_claude",
        "claude",
        {
            "provider": "bedrock_claude",
            "id": "anthropic.claude-sonnet-5-5",
            "extra_kwargs": {
                "aws_region": "us-east-1",
                "aws_access_key": "parity-access-key",
                "aws_secret_key": "parity-secret-key",
            },
        },
        reasoning_kwargs=_CLAUDE_REASONING,
        prompt_cache_config={},
        extra_scenarios=_CLAUDE_EXTRAS,
    ),
    Family(
        "gemini",
        "gemini",
        {"provider": "google", "id": "gemini-3.8-flash", "api_key": _KEY},
        reasoning_kwargs=_GEMINI_REASONING,
    ),
    Family(
        "gemini_vertexai",
        "gemini",
        {
            "provider": "google",
            "id": "gemini-3.8-flash",
            "extra_kwargs": {"vertexai": True, "project_id": "parity-project", "location": "us-central1"},
        },
        reasoning_kwargs=_GEMINI_REASONING,
    ),
    Family("ollama", "ollama", {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://localhost:11434"}),
    Family("groq", "chat", {"provider": "groq", "id": "openai/gpt-oss-120b", "api_key": _KEY}, usage_style="groq"),
    Family("cerebras", "chat", {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": _KEY}, usage_style="cerebras"),
)

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SYSTEM = "You are Parity, a concise travel assistant used by the MindRoom provider goldens."
SYSTEM_WITH_SESSION_CONTEXT = (
    "You are Parity, a concise travel assistant used by the MindRoom provider goldens.\n\n"
    f"{SESSION_CONTEXT_BOUNDARY}\nToday is Tuesday, October 20, 2026 (UTC).\n</mindroom_session_context>"
)
PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
WAV_EMPTY = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA="
PDF_TINY = base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode()
TEXT_NOTE = base64.b64encode(b"Packing list: umbrella, scarf, walking shoes.\n").decode()

WEATHER_TOOL = Function(
    name="get_weather",
    description="Return the forecast for one city.",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name."},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "Temperature unit."},
        },
        "required": ["city"],
    },
).to_dict()
NOTES_TOOL = Function(
    name="search_notes",
    description="Search the user's travel notes.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum results."},
            "query": {"type": "string", "description": "Search text."},
        },
        "required": ["query"],
    },
).to_dict()
ORDER_TOOL = Function(
    name="lookup_order",
    description="Look up one order by id.",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
).to_dict()
INVOICE_TOOL = Function(
    name="send_invoice",
    description="Email the invoice for one order.",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
).to_dict()
TOOL_SEARCH_TOOL = Function(
    name="tool_search",
    description="Search the deferred tools by keyword.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
).to_dict()
CALL_TOOL_TOOL = Function(
    name="call_tool",
    description="Call a deferred tool returned by tool_search.",
    parameters={
        "type": "object",
        "properties": {"arguments": {"type": "object"}, "name": {"type": "string"}},
        "required": ["name"],
    },
).to_dict()
DEFERRED_TOOL_NAMES = ("lookup_order", "send_invoice")


@dataclass(frozen=True)
class Case:
    """One scenario's request inputs and canned reply before the family encodes them."""

    messages: list[dict[str, Any]]
    reply: Reply
    tools: tuple[dict[str, Any], ...] = ()
    model_config: dict[str, Any] = field(default_factory=dict)
    deferred_tool_names: tuple[str, ...] = ()
    portable_replay: bool | None = None
    output_schema: type[BaseModel] | None = None


def _system(text: str = SYSTEM) -> dict[str, Any]:
    return {"role": "system", "content": text}


def _user(text: str, **media: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": text, **media}


def _as_history(message: dict[str, Any]) -> dict[str, Any]:
    if message["role"] == "system":
        return message
    return {**message, "from_history": True}


def _stored_assistant(fixture: dict[str, Any]) -> dict[str, Any]:
    """Return a golden's parsed reply as it is stored and replayed in a later turn.

    The run loop resolves portable ``call_tool`` calls to the deferred function before storing them.
    """
    message = {key: value for key, value in deepcopy(fixture["parsed"]["message"]).items() if key != "citations"}
    if fixture["parsed"]["metrics"]:
        message["metrics"] = deepcopy(fixture["parsed"]["metrics"])
    if calls := message.get("tool_calls"):
        calls = resolve_call_tool_calls(calls, frozenset(DEFERRED_TOOL_NAMES))
        for number, call in enumerate(calls, start=1):
            if call["id"] == GENERATED_ID:
                call["id"] = f"parity-generated-call-{number}"
        message["tool_calls"] = calls
    return message


def _tool_result(call: dict[str, Any], content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": content,
        "tool_call_id": call["id"],
        "tool_name": call["function"]["name"],
        "tool_args": json.loads(call["function"]["arguments"]),
        "tool_call_error": False,
    }


def _followup(done: dict[str, dict[str, Any]], scenario: str, *results: str, prompt: str | None = None) -> list[dict]:
    """Continue an earlier golden: its messages and stored reply, tool results, then a new prompt."""
    fixture = done[scenario]
    assistant = _stored_assistant(fixture)
    calls = assistant.get("tool_calls") or []
    messages = [_as_history(message) for message in fixture["input"]["messages"]]
    messages.append(_as_history(assistant))
    messages.extend(_as_history(_tool_result(call, content)) for call, content in zip(calls, results, strict=True))
    if prompt is not None:
        messages.append(_user(prompt))
    return messages


_PROMPT = "What should I pack for Paris tomorrow?"
_ANSWER = "Pack a light rain jacket and comfortable shoes."
_WEATHER = Call("weather", "get_weather", {"city": "Paris", "unit": "celsius"})
_NOTES = Call("notes", "search_notes", {"query": "Paris packing list"})
_ORDER = Call("order", "lookup_order", {"order_id": "A-1001"})
_CALL_ORDER = Call("order", "call_tool", {"name": "lookup_order", "arguments": {"order_id": "A-1001"}})
_ORDER_RESULT = '{"order_id": "A-1001", "status": "shipped"}'
_WEATHER_RESULT = '{"city": "Paris", "forecast": "light rain", "high_c": 14}'
_NOTES_RESULT = '[{"title": "Paris 2025", "text": "Bring the blue scarf."}]'


def _call(family: Family, call: Call, *, omit_gemini_id: bool = False) -> Call:
    """Drop the call id where providers omit it: always on Ollama, and on Gemini API calls without ids."""
    omit = family.wire == "ollama" or (omit_gemini_id and family.wire == "gemini")
    return Call(None, call.name, call.arguments) if omit else call


def _foreign_tool_turn(family: Family) -> list[dict[str, Any]]:
    """Return a stored tool turn another provider answered, as a thread has after a model switch.

    Claude routes get an OpenAI Chat turn; every other route gets a Claude turn.
    """
    arguments = {"city": "Paris", "unit": "celsius"}
    call = {"type": "function", "function": {"name": "get_weather", "arguments": json.dumps(arguments)}}
    if family.wire == "claude":
        call_id = "call_ForeignParityWeather01"
        tool_call = {
            "provider_data": {
                "id": "chatcmpl-foreign",
                "mindroom_model": {"id": "gpt-6-astra", "provider": "OpenAI Chat"},
            },
        }
        answer = {
            "provider_data": {
                "id": "chatcmpl-foreign-2",
                "mindroom_model": {"id": "gpt-6-astra", "provider": "OpenAI Chat"},
            },
        }
    else:
        call_id = "toolu_01ForeignParityWeather"
        tool_call = {
            "provider_data": {
                "content_blocks": [{"id": call_id, "input": arguments, "name": "get_weather", "type": "tool_use"}],
                "mindroom_model": {"id": "claude-sonnet-5-5", "provider": "Anthropic"},
                "stop_reason": "tool_use",
            },
        }
        answer = {
            "provider_data": {
                "content_blocks": [{"text": _ANSWER, "type": "text"}],
                "mindroom_model": {"id": "claude-sonnet-5-5", "provider": "Anthropic"},
                "stop_reason": "end_turn",
            },
        }
    assistant_call = {"role": "assistant", "tool_calls": [{"id": call_id, **call}], **tool_call}
    return [
        _as_history(_user(_PROMPT)),
        _as_history(assistant_call),
        _as_history(_tool_result(assistant_call["tool_calls"][0], _WEATHER_RESULT)),
        _as_history({"role": "assistant", "content": _ANSWER, **answer}),
    ]


def _claude_tool_search_blocks() -> tuple[dict[str, Any], ...]:
    return (
        {
            "type": "server_tool_use",
            "id": "srvtoolu_parity",
            "name": "tool_search_tool_regex",
            "input": {"pattern": "order"},
        },
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_parity",
            "content": {
                "type": "tool_search_tool_search_result",
                "tool_references": [{"type": "tool_reference", "tool_name": "lookup_order"}],
            },
        },
    )


def _responses_tool_search_items() -> tuple[dict[str, Any], ...]:
    return (
        {
            "id": "ts_parity",
            "type": "tool_search_call",
            "call_id": "tsc_parity",
            "arguments": {"queries": ["order"]},
            "execution": "server",
            "status": "completed",
        },
        {
            "id": "tso_parity",
            "type": "tool_search_output",
            "call_id": "tsc_parity",
            "execution": "server",
            "status": "completed",
            "tools": [{"type": "function", "name": "lookup_order", "parameters": {"type": "object"}}],
        },
    )


def _media_case(kind: str) -> Case:
    media = {
        "image": {"images": [{"id": "parity-image", "content": PNG_1X1, "mime_type": "image/png", "format": "png"}]},
        "file": {
            "files": [
                {"id": "parity-file", "content": PDF_TINY, "mime_type": "application/pdf", "filename": "itinerary.pdf"},
            ],
        },
        "audio": {"audio": [{"id": "parity-audio", "content": WAV_EMPTY, "mime_type": "audio/wav"}]},
    }[kind]
    return Case([_system(), _user(f"Use the attached {kind} to plan my packing.", **media)], Reply(text=_ANSWER))


# Responses replay goldens and the tool-call turns that seed their history.
_RESPONSES_REPLAY_SEEDS = {
    "responses_explicit_replay": "reasoning_tool_call",
    "responses_store_false": "responses_store_false_turn",
}


def _responses_replay_case(name: str, family: Family, messages: list[dict[str, Any]], reply: Reply) -> Case:
    """Replay without server state: history budgeting turns on portable replay, or the model sets ``store: false``."""
    store_false = name.startswith("responses_store_false")
    extra_kwargs = {"store": False} if store_false else {}
    return Case(
        messages,
        reply,
        tools=(WEATHER_TOOL, NOTES_TOOL),
        model_config={"extra_kwargs": {**extra_kwargs, **(family.reasoning_kwargs or {})}},
        portable_replay=None if store_false else True,
    )


def _scenario(name: str, family: Family, done: dict[str, dict[str, Any]]) -> Case:  # noqa: C901, PLR0911, PLR0912
    weather_tools = (WEATHER_TOOL, NOTES_TOOL)
    if name == "plain_turn":
        return Case([_system(), _user(_PROMPT)], Reply(text=_ANSWER))
    if name == "system_history":
        return Case(_followup(done, "plain_turn", prompt="And for the evening?"), Reply(text="Add a warm layer."))
    if name == "tool_definitions":
        reply = Reply(calls=(_call(family, _WEATHER, omit_gemini_id=True),))
        return Case([_system(), _user(_PROMPT)], reply, tools=weather_tools)
    if name == "foreign_tool_history":
        messages = [_system(), *_foreign_tool_turn(family), _user("And for the evening?")]
        return Case(messages, Reply(text="Add a warm layer."), tools=weather_tools)
    if name == "tool_results_history":
        return Case(_followup(done, "tool_definitions", _WEATHER_RESULT), Reply(text=_ANSWER), tools=weather_tools)
    if name == "parallel_tool_calls":
        calls = (_call(family, _WEATHER), _call(family, _NOTES))
        return Case([_system(), _user(_PROMPT)], Reply(calls=calls), tools=weather_tools)
    if name == "parallel_tool_results":
        messages = _followup(done, "parallel_tool_calls", _WEATHER_RESULT, _NOTES_RESULT)
        return Case(messages, Reply(text=_ANSWER), tools=weather_tools)
    if name in {"image_input", "file_input", "audio_input"}:
        return _media_case(name.removesuffix("_input"))
    if name == "reasoning":
        reply = Reply(text=_ANSWER, reasoning="Rain is likely, so pack for wet weather.", reasoning_tokens=7)
        return Case(
            [_system(), _user(_PROMPT)],
            reply,
            model_config={"extra_kwargs": family.reasoning_kwargs or {}},
        )
    if name == "reasoning_replay":
        return Case(
            _followup(done, "reasoning", prompt="And for the evening?"),
            Reply(text="Add a warm layer."),
            model_config={"extra_kwargs": family.reasoning_kwargs or {}},
        )
    if name == "claude_citations":
        note = {"id": "parity-note", "content": TEXT_NOTE, "mime_type": "text/plain", "filename": "packing.txt"}
        citation = {
            "type": "char_location",
            "cited_text": "Packing list: umbrella, scarf, walking shoes.",
            "document_index": 0,
            "document_title": "packing.txt",
            "start_char_index": 0,
            "end_char_index": 45,
        }
        reply = Reply(text="Your list says to bring an umbrella.", claude_citations=(citation,))
        return Case([_system(), _user("What does my packing note say?", files=[note])], reply)
    if name == "claude_citations_replay":
        return Case(_followup(done, "claude_citations", prompt="Anything else?"), Reply(text="Walking shoes."))
    if name == "claude_server_tool_blocks":
        reply = Reply(calls=(_ORDER,), claude_blocks=_claude_tool_search_blocks())
        return Case(
            [_system(), _user("Where is order A-1001?")],
            reply,
            tools=(WEATHER_TOOL, ORDER_TOOL, INVOICE_TOOL),
            deferred_tool_names=DEFERRED_TOOL_NAMES,
        )
    if name == "claude_server_tool_blocks_replay":
        return Case(
            _followup(done, "claude_server_tool_blocks", _ORDER_RESULT),
            Reply(text="Order A-1001 has shipped."),
            tools=(WEATHER_TOOL, ORDER_TOOL, INVOICE_TOOL),
            deferred_tool_names=DEFERRED_TOOL_NAMES,
        )
    if name == "responses_continuation":
        return Case(_followup(done, "plain_turn", prompt="And for the evening?"), Reply(text="Add a warm layer."))
    if name in {"reasoning_tool_call", "responses_store_false_turn"}:
        reply = Reply(reasoning="Check the forecast first.", calls=(_WEATHER,), reasoning_tokens=5)
        return _responses_replay_case(name, family, [_system(), _user(_PROMPT)], reply)
    if name in _RESPONSES_REPLAY_SEEDS:
        messages = _followup(done, _RESPONSES_REPLAY_SEEDS[name], _WEATHER_RESULT)
        return _responses_replay_case(name, family, messages, Reply(text=_ANSWER))
    if name == "structured_output":
        suggestion = {"entity_name": "travel_agent", "reasoning": "The message asks about packing for a trip."}
        return Case(
            [_system("Route the message to the best agent."), _user(_PROMPT)],
            Reply(text=json.dumps(suggestion)),
            output_schema=_RoutingSuggestion,
        )
    if name == "prompt_cache":
        reply = Reply(text=_ANSWER, cached_tokens=30, cache_write_tokens=8)
        return Case(
            [_system(SYSTEM_WITH_SESSION_CONTEXT), _user(_PROMPT)],
            reply,
            tools=weather_tools,
            model_config=family.prompt_cache_config or {},
        )
    if name == "prompt_cache_disabled":
        return Case(
            [_system(SYSTEM_WITH_SESSION_CONTEXT), _user(_PROMPT)],
            Reply(text=_ANSWER),
            tools=weather_tools,
            model_config=_CLAUDE_PROMPT_CACHE_OFF,
        )
    if name in {"deferred_tool_search", "deferred_tool_search_replay"}:
        tools = (WEATHER_TOOL, ORDER_TOOL, INVOICE_TOOL)
        if family.deferred_mode == "portable":
            tools = (WEATHER_TOOL, TOOL_SEARCH_TOOL, CALL_TOOL_TOOL, ORDER_TOOL, INVOICE_TOOL)
            reply = Reply(calls=(_call(family, _CALL_ORDER),))
        elif family.wire == "responses":
            reply = Reply(calls=(_ORDER,), responses_items=_responses_tool_search_items())
        else:
            # Native Claude tool search blocks have their own claude_server_tool_blocks goldens.
            reply = Reply(text="I can look that up.")
        if name == "deferred_tool_search":
            return Case(
                [_system(), _user("Where is order A-1001?")],
                reply,
                tools=tools,
                deferred_tool_names=DEFERRED_TOOL_NAMES,
            )
        return Case(
            _followup(done, "deferred_tool_search", _ORDER_RESULT),
            Reply(text="Order A-1001 has shipped."),
            tools=tools,
            deferred_tool_names=DEFERRED_TOOL_NAMES,
        )
    msg = f"unknown scenario {name}"
    raise ValueError(msg)


_BASE_SCENARIOS = (
    "plain_turn",
    "system_history",
    "tool_definitions",
    "tool_results_history",
    "parallel_tool_calls",
    "parallel_tool_results",
    "foreign_tool_history",
    "image_input",
    "file_input",
    "audio_input",
)


def family_scenarios(family: Family) -> list[str]:
    """Return the scenarios one family supports, ordered so seeds precede their follow-ups."""
    names = list(_BASE_SCENARIOS)
    if family.reasoning_kwargs is not None:
        names += ["reasoning", "reasoning_replay"]
    names += family.extra_scenarios
    names.append("structured_output")
    if family.prompt_cache_config is not None:
        names.append("prompt_cache")
    names.append("deferred_tool_search")
    if family.deferred_mode == "portable" or family.wire == "responses":
        names.append("deferred_tool_search_replay")
    return names


def _merged_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if key == "extra_kwargs":
            merged["extra_kwargs"] = {**merged.get("extra_kwargs", {}), **value}
        else:
            merged[key] = value
    if not merged.get("extra_kwargs"):
        merged.pop("extra_kwargs", None)
    return merged


def _case_input(family: Family, case: Case) -> dict[str, Any]:
    case_input: dict[str, Any] = {
        "model_config": _merged_config(family.model_config, case.model_config),
        "messages": case.messages,
        "tools": list(case.tools),
        "tool_choice": None,
        "response_format": None,
        "deferred_tool_search": {"mode": family.deferred_mode, "tool_names": list(case.deferred_tool_names)}
        if case.deferred_tool_names
        else None,
        "portable_replay": case.portable_replay,
        "stream": case.output_schema is None,
    }
    if case.output_schema is not None:
        case_input["response_format"] = resolved_response_format(case_input, case.output_schema)
    return _json_roundtrip(case_input)


def capture_family(family: Family) -> dict[str, dict[str, Any]]:
    """Capture every golden of one family, in scenario order."""
    done: dict[str, dict[str, Any]] = {}
    for name in family_scenarios(family):
        case = _scenario(name, family, done)
        case_input = _case_input(family, case)
        response_raw = _json_roundtrip(_ENCODERS[family.wire](case.reply, family, stream=case_input["stream"]))
        result = run_golden(case_input, response_raw)
        done[name] = {
            "family": family.name,
            "scenario": name,
            "input": case_input,
            "request": result["request"],
            "response_raw": response_raw,
            "parsed": result["parsed"],
        }
    return done


def fixture_text(fixture: dict[str, Any]) -> str:
    """Serialize one golden as pretty, key-sorted JSON."""
    return json.dumps(fixture, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    """Rewrite the provider goldens."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("families", nargs="*", help="Family names to capture; default all.")
    args = parser.parse_args()
    selected = [family for family in FAMILIES if not args.families or family.name in args.families]
    for family in selected:
        family_dir = FIXTURE_ROOT / family.name
        family_dir.mkdir(parents=True, exist_ok=True)
        for stale in family_dir.glob("*.json"):
            stale.unlink()
        for name, fixture in capture_family(family).items():
            (family_dir / f"{name}.json").write_text(fixture_text(fixture), encoding="utf-8")
        print(f"{family.name}: {len(family_scenarios(family))} goldens")


if __name__ == "__main__":
    main()
