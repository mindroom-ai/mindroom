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
from mindroom.llm.provider import TurnCompleted
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider
from tests.providers.golden_support import Replay, encoded_response, replay_through_pipeline

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest
    from tests.providers.golden_support import Wire

FAMILIES = ("anthropic", "vertexai_claude", "bedrock_claude")
# Provider SDKs read these environment variables directly; the harness hides them from the run.
_PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "AWS_", "CLOUD_ML_", "GOOGLE_")


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


def json_reply(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
) -> httpx.Response:
    """Return the non-streaming reply with ``blocks``."""
    return httpx.Response(200, json=claude_message(blocks, stop_reason=stop_reason, usage=usage))


def sse_reply(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
) -> httpx.Response:
    """Stream the blocks the way the API does: a start event, deltas per block, the stop reason, the stop."""
    final_usage = usage or CLAUDE_USAGE
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": claude_message([], stop_reason="end_turn", usage={**final_usage, "output_tokens": 1})
            | {"stop_reason": None},
        },
    ]
    for index, authored in enumerate(blocks):
        # Popping the helper-only keys below must leave the caller's blocks reusable.
        block = dict(authored)
        kind = block["type"]
        deltas: list[dict[str, Any]]
        if kind == "text":
            start: dict[str, Any] = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif kind == "tool_use":
            partial_json = block.pop("partial_json", None)
            start = {**block, "input": {}}
            deltas = [
                {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"]) if partial_json is None else partial_json,
                },
            ]
        else:
            # Other blocks arrive whole, unless the block names deltas of its own.
            deltas = block.pop("deltas", [])
            start = block
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": final_usage["output_tokens"]},
        },
    )
    events.append({"type": "message_stop"})
    return encoded_response(
        {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "sse": [{"event": event["type"], "data": event} for event in events],
        },
    )


def route_provider(route: str, tmp_path: Path, wire: Wire, **config: Any) -> ClaudeProvider:  # noqa: ANN401
    """Build the adapter of one Claude route over ``wire``, with ``config`` merged into the route's model config."""
    model_config = {**ROUTE_CONFIGS[route], **config}
    if "extra_kwargs" in config:
        model_config["extra_kwargs"] = {**ROUTE_CONFIGS[route].get("extra_kwargs", {}), **config["extra_kwargs"]}
    return provider_for(model_config, tmp_path, wire.client)


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
