"""OpenAI endpoint resolution must reach the request the SDK client sends."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import httpx
import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.pipeline.wrapped import bare_adapter
from mindroom.providers.registry import build_model_provider
from tests.providers.golden_support import Wire

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("api", [None, "chat_completions", "responses"])
@pytest.mark.parametrize(
    ("endpoint_source", "expected_url"),
    [
        ("default", "https://api.openai.com/v1/"),
        ("dotenv", "https://dotenv.example/v1/"),
        ("dotenv_with_null_model_url", "https://dotenv.example/v1/"),
        ("process", "https://process.example/v1/"),
        ("process_snapshot", "https://process.example/v1/"),
        ("model", "https://model.example/v1/"),
        ("client_params", "https://client.example/v1/"),
    ],
)
@pytest.mark.asyncio
async def test_openai_endpoint_precedence_reaches_sdk_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: Literal["chat_completions", "responses"] | None,
    endpoint_source: str,
    expected_url: str,
) -> None:
    """Runtime endpoints reach SDK construction, while explicit endpoints keep precedence."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    if endpoint_source != "default":
        (tmp_path / ".env").write_text("OPENAI_BASE_URL=https://dotenv.example/v1\n", encoding="utf-8")
    process_env = {}
    if endpoint_source in {"process", "process_snapshot", "model", "client_params"}:
        process_env["OPENAI_BASE_URL"] = "https://process.example/v1"
        monkeypatch.setenv("OPENAI_BASE_URL", "https://process.example/v1")
    runtime_paths = resolve_runtime_paths(config_path=config_path, process_env=process_env)
    if endpoint_source == "process_snapshot":
        monkeypatch.delenv("OPENAI_BASE_URL")
    extra_kwargs: dict[str, object] = {"api_key": "dummy-key"}
    if endpoint_source == "dotenv_with_null_model_url":
        extra_kwargs["base_url"] = None
    elif endpoint_source in {"model", "client_params"}:
        extra_kwargs["base_url"] = "https://model.example/v1"
    if endpoint_source == "client_params":
        extra_kwargs["client_params"] = {"base_url": "https://client.example/v1"}
    config = Config(
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", api=api, extra_kwargs=extra_kwargs),
        },
    )
    wire = Wire(httpx.Response(400, json={"error": {"message": "bad", "type": "invalid_request_error"}}))
    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", wire.client.send)
    provider = build_model_provider(config, runtime_paths)
    responses = api == "responses" or (api is None and endpoint_source == "default")
    assert isinstance(bare_adapter(provider), ResponsesAdapter if responses else ChatAdapter)

    with pytest.raises(ProviderError):
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=False))

    [request] = wire.requests
    assert str(request.url) == expected_url + ("responses" if responses else "chat/completions")
    if endpoint_source in {"default", "dotenv", "dotenv_with_null_model_url", "process_snapshot"}:
        assert "OPENAI_BASE_URL" not in os.environ
