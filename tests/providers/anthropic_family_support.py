"""Harness for the Claude-family parity tests: build the owned adapter for a golden and answer its HTTP request.

The goldens under ``tests/fixtures/agno_parity/providers/{anthropic,vertexai_claude,bedrock_claude}`` pin the
request Agno 3.0.9 plus MindRoom's patches sent and the message they parsed; ``README.md`` beside them
documents the format. Request building, the pipeline stages, and normalization are shared in ``golden_support``.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from google.oauth2.credentials import Credentials

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.provider import TurnCompleted
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider
from tests.providers.golden_support import (
    Replay,
    claude_stream_events,
    encoded_response,
    hide_provider_environment,
    httpx2_transport,
    replay_through_pipeline,
    sse_response,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

    from mindroom.llm.provider import ModelRequest, NativeCompactionReplay
    from tests.providers.golden_support import SentRequest, Wire

FAMILIES = ("anthropic", "vertexai_claude", "bedrock_claude")


@contextmanager
def isolated_provider_environment() -> Iterator[None]:
    """Apply ``hide_provider_environment`` for the block, for callers without the ``monkeypatch`` fixture."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        hide_provider_environment(monkeypatch)
        yield


def provider_for(model_config: dict[str, Any], root: Path, http_client: httpx2.AsyncClient) -> ClaudeProvider:
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
    recorded: list[SentRequest] = []

    def respond(request: SentRequest) -> httpx.Response:
        recorded.append(request)
        return encoded_response(fixture["response_raw"])

    with isolated_provider_environment():
        async with httpx2.AsyncClient(transport=httpx2_transport(respond)) as http_client:
            provider = provider_for(fixture["input"]["model_config"], root, http_client)
            return await replay_through_pipeline(provider, fixture["input"], recorded)


ROUTE_CONFIGS: dict[str, dict[str, Any]] = {
    "anthropic": {"provider": "anthropic", "id": "claude-sonnet-5-5", "api_key": "test-key"},
    "vertexai_claude": {
        "provider": "vertexai_claude",
        "id": "claude-sonnet-5-5",
        "extra_kwargs": {"project_id": "test-project", "region": "us-east5"},
    },
    "bedrock_claude": {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5-5",
        "extra_kwargs": {"aws_region": "us-east-1", "aws_access_key": "test-access", "aws_secret_key": "test-secret"},
    },
}
CLAUDE_USAGE = {"input_tokens": 40, "output_tokens": 9, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}


def claude_message(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> dict:
    """Return a finished Messages API response with ``blocks``."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or CLAUDE_USAGE,
    }


def sse_reply(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
    context_management: dict[str, Any] | None = None,
) -> httpx.Response:
    """Stream the blocks the way the API does (``claude_stream_events``), with the context editing the response applied."""
    message = claude_message(blocks, stop_reason=stop_reason, usage=usage)
    return sse_response(claude_stream_events(message, context_management=context_management), named=True)


def route_provider(route: str, tmp_path: Path, wire: Wire, **config: Any) -> ClaudeProvider:  # noqa: ANN401
    """Build the adapter of one Claude route over ``wire``, with ``config`` merged into the route's model config."""
    model_config = {**ROUTE_CONFIGS[route], **config}
    if "extra_kwargs" in config:
        model_config["extra_kwargs"] = {**ROUTE_CONFIGS[route].get("extra_kwargs", {}), **config["extra_kwargs"]}
    return provider_for(model_config, tmp_path, wire.client)


def native_settings(
    provider: ClaudeProvider,
    *,
    threshold: int | None = 60000,
    history_generation: str = "",
    allow_authored: bool = False,
) -> NativeCompactionReplay | None:
    """Return the native-compaction settings history planning asks ``provider`` for.

    The defaults are a managed request whose history has no summary, which carries an empty ``history_generation``.
    """
    return provider.native_compaction_settings(
        threshold=threshold,
        history_generation=history_generation,
        allow_authored=allow_authored,
    )


async def claude_events(provider: ClaudeProvider, request: ModelRequest) -> list[Any]:
    """Return every event of one request, with provider credentials hidden from the SDK."""
    with isolated_provider_environment():
        return [event async for event in provider.stream(request)]


async def claude_turn(provider: ClaudeProvider, request: ModelRequest) -> TurnCompleted:
    """Return the finished turn of one request."""
    events = await claude_events(provider, request)
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed
