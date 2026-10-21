"""Harness for the Claude-family parity tests: build the owned adapter for a golden and answer its HTTP request.

The goldens under ``tests/fixtures/agno_parity/providers/{anthropic,vertexai_claude,bedrock_claude}`` pin the
request Agno 3.0.9 plus MindRoom's patches sent and the message they parsed; ``README.md`` beside them
documents the format. Request building, the pipeline stages, and normalization are shared in ``golden_support``.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
from google.oauth2.credentials import Credentials

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider
from tests.providers.golden_support import Replay, replay_through_pipeline

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FAMILIES = ("anthropic", "vertexai_claude", "bedrock_claude")
# Provider SDKs read these environment variables directly; the harness hides them from the run.
_PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "AWS_", "CLOUD_ML_", "GOOGLE_")


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


async def run_golden(fixture: dict[str, Any], root: Path) -> Replay:
    """Send one golden's request through the pipeline around the owned adapter and return what it produced."""
    recorded: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return encoded_response(fixture["response_raw"])

    with isolated_provider_environment():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            provider = provider_for(fixture["input"]["model_config"], root, http_client)
            return await replay_through_pipeline(
                provider,
                fixture["input"],
                recorded,
                native_compaction_settings=provider.native_compaction_settings,
            )
