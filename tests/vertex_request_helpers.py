"""Capture the request body a configured Vertex AI Claude model sends on the wire."""
# ruff: noqa: S106

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from anthropic import AnthropicVertex
from google.oauth2.credentials import Credentials

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from agno.models.message import Message


def vertex_request_body(tmp_path: Path, messages: list[Message], *, model_id: str = "claude-opus-5") -> dict[str, Any]:
    """Send one request through a Vertex AI Claude model built from config and return its body."""
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model_id,
                "content": [{"type": "text", "text": "Read."}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="vertexai_claude",
                    id=model_id,
                    extra_kwargs={"project_id": "demo-project", "region": "us-central1"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "claude")
    assert isinstance(model, MindroomVertexAIClaude)
    model.client = AnthropicVertex(
        project_id="demo-project",
        region="us-central1",
        credentials=Credentials(token="test-token"),
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    model.response(messages=messages, compression_manager=None)
    return bodies[0]
