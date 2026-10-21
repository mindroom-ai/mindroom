"""Replay the provider wire goldens through the owned OpenAI-family adapters.

A golden (``tests/fixtures/agno_parity/providers/<family>/<scenario>.json``) holds the authored model
config and request inputs, the request Agno sent, the canned reply, and the parsed result. The helpers
here build the owned adapter from the same config, answer its HTTP request with the canned reply, and
return the recorded request and the parsed turn in the golden's normalized form.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ModelTurn, ToolSpec, complete
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.pipeline.compose import wrap_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"
OPENAI_FAMILIES = (
    "openai_chat",
    "openai_responses",
    "codex",
    "azure",
    "openrouter",
    "deepseek",
    "zai",
    "llama_cpp",
    "kimi",
    "groq",
    "cerebras",
)

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
_GENERATED_ID = "<uuid4>"
_TIMING_METRIC_KEYS = ("duration", "time_to_first_token")
_STOP_KEYS = ("stop_reason", "finish_reason", "response_status")
_CODEX_HOME = "<codex_home>"
_KIMI_HOME = "<kimi_home>"
# Far-future expiry so borrowed OAuth tokens never refresh: 2100-01-01T00:00:00Z.
_TOKEN_EXPIRY = 4102444800


def golden_paths(families: tuple[str, ...] = OPENAI_FAMILIES) -> list[Path]:
    """Return every golden of the given families, in a stable order."""
    return sorted(path for family in families for path in (FIXTURE_ROOT / family).glob("*.json"))


def load_golden(path: Path) -> dict[str, Any]:
    """Read one golden file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _unsigned_jwt(claims: dict[str, Any]) -> str:
    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.parity"


def write_cli_logins(root: Path) -> dict[str, str]:
    """Write the Codex and Kimi CLI credential files the golden placeholders stand for."""
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
    return {_CODEX_HOME: str(codex_home), _KIMI_HOME: str(kimi_home)}


class _PacedEvents(httpx.AsyncByteStream):
    """SSE events sent one at a time after a pause each, optionally ending in a dropped connection."""

    def __init__(self, events: list[bytes], *, pause_seconds: float, drop: bool) -> None:
        self._events = events
        self._pause_seconds = pause_seconds
        self._drop = drop

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self._events:
            await asyncio.sleep(self._pause_seconds)
            yield event
        if self._drop:
            msg = "connection dropped"
            raise httpx.ReadError(msg)


def _sse_event(event: dict[str, Any]) -> bytes:
    data = event["data"]
    name = f"event: {event['event']}\n" if "event" in event else ""
    return f"{name}data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode()


def encoded_response(response_raw: dict[str, Any]) -> httpx.Response:
    """Build the HTTP response a golden's ``response_raw`` describes.

    Tests may add ``pause_seconds`` to send each SSE event after a pause, and ``drop_connection`` to end the
    stream with a transport error instead of EOF.
    """
    status, headers = response_raw["status"], response_raw["headers"]
    if "sse" not in response_raw:
        return httpx.Response(status, headers=headers, content=json.dumps(response_raw["json"]).encode())
    events = [_sse_event(event) for event in response_raw["sse"]]
    if "pause_seconds" in response_raw or "drop_connection" in response_raw:
        stream = _PacedEvents(
            events,
            pause_seconds=response_raw.get("pause_seconds", 0.0),
            drop=response_raw.get("drop_connection", False),
        )
        return httpx.Response(status, headers=headers, stream=stream)
    return httpx.Response(status, headers=headers, content=b"".join(events))


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


def _scrub_generated_ids(value: object) -> Any:  # noqa: ANN401 - arbitrary JSON
    if isinstance(value, dict):
        return {key: _scrub_generated_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_generated_ids(item) for item in value]
    if isinstance(value, str) and _UUID4.match(value):
        return _GENERATED_ID
    return value


def normalized_parsed(turn: ModelTurn) -> dict[str, Any]:
    """Return the turn as the golden's ``parsed`` block records it."""
    message = turn.message.to_dict()
    for key in ("id", "created_at"):
        message.pop(key, None)
    metrics = message.pop("metrics", {})
    for key in _TIMING_METRIC_KEYS:
        metrics.pop(key, None)
    provider_data = turn.message.provider_data or {}
    parsed = {
        "message": message,
        "metrics": metrics,
        "stop_reason": next((provider_data[key] for key in _STOP_KEYS if provider_data.get(key) is not None), None),
    }
    return _scrub_generated_ids(json.loads(json.dumps(parsed, sort_keys=True)))


def _response_format(encoded: dict[str, Any] | None) -> Any:  # noqa: ANN401 - dict or pydantic class
    if encoded is None:
        return None
    if encoded["type"] == "dict":
        return encoded["value"]
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    assert schema.model_json_schema() == encoded["json_schema"], f"{encoded['model']} changed since the golden"
    return schema


def _tool_spec(tool: dict[str, Any]) -> ToolSpec:
    # The capture rebuilt each input with Agno's ``Function.from_dict``, which defaults both approval flags to False.
    return ToolSpec(
        name=tool["name"],
        description=tool["description"],
        parameters=tool["parameters"],
        strict=tool.get("strict"),
        requires_confirmation=tool.get("requires_confirmation", False),
        external_execution=tool.get("external_execution", False),
        approval_type=tool.get("approval_type"),
    )


def golden_request(case_input: dict[str, Any]) -> ModelRequest:
    """Return the owned request a golden's input describes."""
    deferred = case_input["deferred_tool_search"]
    return ModelRequest(
        messages=[Message.from_dict(message) for message in case_input["messages"]],
        tools=[_tool_spec(tool) for tool in case_input["tools"]],
        tool_choice=case_input["tool_choice"],
        response_format=_response_format(case_input["response_format"]),
        stream=case_input["stream"],
        deferred_tool_names=frozenset(deferred["tool_names"]) if deferred is not None else frozenset(),
        portable_replay=bool(case_input["portable_replay"]),
    )


@dataclass(frozen=True, slots=True)
class GoldenProvider:
    """An owned provider built from a golden's config, answering HTTP with the golden's canned reply.

    ``provider`` is the adapter inside the request pipeline; ``adapter`` is the bare adapter.
    """

    provider: ModelProvider
    adapter: ModelProvider
    requests: list[httpx.Request]


def golden_provider(
    golden: dict[str, Any],
    root: Path,
    *,
    response_raw: dict[str, Any] | None = None,
    idle_timeout_seconds: float | None = None,
) -> GoldenProvider:
    """Build the owned adapter and pipeline for a golden's model config under ``root``."""
    placeholders = write_cli_logins(root)
    config = json.loads(json.dumps(golden["input"]["model_config"]))
    for key, value in list((config.get("extra_kwargs") or {}).items()):
        if isinstance(value, str) and value in placeholders:
            config["extra_kwargs"][key] = placeholders[value]
    reply = response_raw if response_raw is not None else golden["response_raw"]
    recorded: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return encoded_response(reply)

    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={}),
        model_name="default",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    adapter = build_provider(ModelConfig(**config), runtime)
    provider = wrap_provider(
        adapter,
        idle_timeout_seconds=idle_timeout_seconds,
        media_fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT,
        request_log=None,
    )
    return GoldenProvider(provider=provider, adapter=adapter, requests=recorded)


async def replay_golden(golden: dict[str, Any], root: Path) -> tuple[dict[str, Any], dict[str, Any], ModelTurn]:
    """Send a golden's request through the owned provider; return the normalized request, parsed turn, and turn."""
    built = golden_provider(golden, root)
    turn = await complete(built.provider, golden_request(golden["input"]))
    assert len(built.requests) == 1, f"expected one provider request, got {len(built.requests)}"
    await built.requests[0].aread()
    return normalized_request(built.requests[0]), normalized_parsed(turn), turn
