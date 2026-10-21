"""Replay the provider wire goldens through the owned OpenAI-family adapters.

The helpers here build the owned adapter from a golden's model config through ``build_provider``, answer its
HTTP request with the canned reply, and hand the request to the shared golden harness in ``golden_support``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.openai.responses import ResponsesAdapter
from tests.providers.golden_support import PacedBytes, Replay, pipeline_provider, replay_through_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, NativeCompactionReplay

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
_CODEX_HOME = "<codex_home>"
_KIMI_HOME = "<kimi_home>"
# Far-future expiry so borrowed OAuth tokens never refresh: 2100-01-01T00:00:00Z.
_TOKEN_EXPIRY = 4102444800


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
        stream = PacedBytes(
            events,
            pause_seconds=response_raw.get("pause_seconds", 0.0),
            drop=response_raw.get("drop_connection", False),
        )
        return httpx.Response(status, headers=headers, stream=stream)
    return httpx.Response(status, headers=headers, content=b"".join(events))


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
    return GoldenProvider(
        provider=pipeline_provider(adapter, idle_timeout_seconds=idle_timeout_seconds),
        adapter=adapter,
        requests=recorded,
    )


def _native_compaction_settings(adapter: ModelProvider) -> Callable[..., NativeCompactionReplay | None] | None:
    """Return the adapter's replay-settings method where the route has one (Responses and Codex)."""
    return adapter.native_compaction_settings if isinstance(adapter, ResponsesAdapter) else None


async def replay_golden(golden: dict[str, Any], root: Path) -> Replay:
    """Send a golden's request through the owned provider pipeline and return what it sent and produced."""
    built = golden_provider(golden, root)
    return await replay_through_pipeline(
        built.adapter,
        golden["input"],
        built.requests,
        native_compaction_settings=_native_compaction_settings(built.adapter),
    )
