"""Replay the C1 provider goldens of the Gemini and Ollama routes through the owned adapters.

The harness builds the owned adapter from the golden's model config, answers its single HTTP request with
the golden's canned reply through an httpx mock transport, and hands the request to the shared golden harness
in ``golden_support``, which runs it through the production pipeline stages.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from google.oauth2.credentials import Credentials

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.gemini.adapter import build_provider as build_gemini_provider
from mindroom.providers.ollama_adapter import build_provider as build_ollama_provider
from tests.providers.golden_support import FIXTURE_ROOT, Replay, normalized_request, replay_through_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

FAMILIES = ("gemini", "gemini_vertexai", "ollama")
_PROVIDER_ENV_PREFIXES = ("GEMINI_", "GOOGLE_", "OLLAMA_")


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


def _encoded_response(golden: Golden) -> httpx.Response:
    """Build the canned reply: an SSE or ndjson stream, or the JSON body of a non-streaming reply."""
    raw = golden.data["response_raw"]
    if "sse" in raw:
        content = "".join(f"data: {json.dumps(event['data'])}\n\n" for event in raw["sse"]).encode()
        return httpx.Response(raw["status"], headers={"content-type": "text/event-stream"}, content=content)
    if "ndjson" in raw:
        content = b"".join(json.dumps(event).encode() + b"\n" for event in raw["ndjson"])
        return httpx.Response(raw["status"], headers={"content-type": "application/x-ndjson"}, content=content)
    return httpx.Response(raw["status"], json=raw["json"])


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


async def replay(golden: Golden, root: Path) -> Replay:
    """Send one golden's request through the pipeline around the owned adapter and return what it produced."""
    recorded: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return _encoded_response(golden)

    adapter = build_adapter(golden.data["input"]["model_config"], root, respond)
    return await replay_through_pipeline(adapter, golden.data["input"], recorded)


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
    """Send ``request`` through the bare adapter for ``model_config`` and return its HTTP request and events."""
    recorded: list[httpx.Request] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        recorded.append(http_request)
        if isinstance(response, Exception):
            raise response
        return response

    adapter = build_adapter(model_config, root, respond)
    events = [event async for event in adapter.stream(request)]
    return normalized_request(recorded[0]), events
