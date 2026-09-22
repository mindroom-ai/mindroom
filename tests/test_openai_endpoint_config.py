"""OpenAI endpoint resolution must reach both SDK clients without network access."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import httpx
import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import MindRoomOpenAIChat, MindRoomOpenAIResponses

if TYPE_CHECKING:
    from pathlib import Path


def _reject_request(request: httpx.Request) -> httpx.Response:
    msg = f"Unexpected HTTP request: {request.method} {request.url}"
    raise AssertionError(msg)


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
    model = get_model_instance(config, runtime_paths)
    expected_class = (
        MindRoomOpenAIResponses
        if api == "responses" or (api is None and endpoint_source == "default")
        else MindRoomOpenAIChat
    )
    assert isinstance(model, expected_class)

    transport = httpx.MockTransport(_reject_request)
    with httpx.Client(transport=transport) as http_client:
        model.http_client = http_client
        client = model.get_client()
        sync_url = str(client.base_url)
        client.close()
    async with httpx.AsyncClient(transport=transport) as async_http_client:
        model.http_client = async_http_client
        async_client = model.get_async_client()
        async_url = str(async_client.base_url)
        await async_client.close()

    assert (sync_url, async_url) == (expected_url, expected_url)
    if endpoint_source in {"default", "dotenv", "dotenv_with_null_model_url", "process_snapshot"}:
        assert "OPENAI_BASE_URL" not in os.environ
