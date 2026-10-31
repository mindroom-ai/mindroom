"""Each provider route's token counter: which counting endpoint it calls, and when its own tokenizer runs locally."""
# ruff: noqa: D103

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.token_counting.counts import count_exact
from mindroom.token_counting.families import load_o200k
from tests.providers.anthropic_family_support import ROUTE_CONFIGS, provider_for
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.openai_family_support import provider_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider


class _Wire:
    """Answer every counting request with the length of its text plus ``framing`` and record it."""

    def __init__(self, reply: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.reply = reply
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, request: httpx2.Request | httpx.Request) -> httpx2.Response | httpx.Response:
        body = json.loads(request.content)
        self.requests.append((str(request.url), body, dict(request.headers)))
        response = httpx2.Response if isinstance(request, httpx2.Request) else httpx.Response
        return response(200, json=self.reply(body))


def _claude_text(body: dict[str, Any]) -> str:
    return body["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "path"),
    [
        ("anthropic", "/v1/messages/count_tokens"),
        ("vertexai_claude", "/publishers/anthropic/models/count-tokens:rawPredict"),
        ("bedrock_claude", "/v1/messages/count_tokens"),
    ],
)
async def test_claude_counts_through_count_tokens_without_framing(tmp_path: Path, route: str, path: str) -> None:
    wire = _Wire(lambda body: {"input_tokens": len(_claude_text(body)) + 8})
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(wire)) as http_client:
        provider = provider_for(ROUTE_CONFIGS[route], tmp_path, http_client)
        counter = provider.token_counter
        assert counter.count is not None
        assert not counter.exact_locally
        assert await counter.count("hello world") == len("hello world")
    assert all(url.split("?")[0].endswith(path) for url, _body, _headers in wire.requests)
    assert sorted(body["messages"][0]["content"] for _url, body, _headers in wire.requests) == ["a", "hello world"]
    assert all(body["messages"][0]["role"] == "user" for _url, body, _headers in wire.requests)


@pytest.mark.asyncio
async def test_gemini_counts_through_count_tokens(tmp_path: Path) -> None:
    def reply(body: dict[str, Any]) -> dict[str, Any]:
        return {"totalTokens": len(body["contents"][0]["parts"][0]["text"])}

    wire = _Wire(reply)
    provider = build_adapter({"provider": "google", "id": "gemini-3.8-flash", "api_key": "test-key"}, tmp_path, wire)
    counter = provider.token_counter
    assert counter.count is not None
    assert await counter.count("hello world") == len("hello world")
    assert wire.requests[0][0].endswith("/models/gemini-3.8-flash:countTokens")


def _openai_provider(tmp_path: Path, wire: _Wire, **model: Any) -> ModelProvider:  # noqa: ANN401
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(wire))
    return build_openai_provider(ModelConfig(**model), provider_runtime(runtime_paths, http_client=http_client))


@pytest.mark.asyncio
async def test_responses_counts_through_input_tokens_on_openai_only(tmp_path: Path) -> None:
    wire = _Wire(lambda body: {"object": "response.input_tokens", "input_tokens": len(body["input"]) + 7})
    provider = _openai_provider(tmp_path, wire, provider="openai", id="gpt-6-astra", api_key="test-key")
    counter = provider.token_counter
    assert counter.count is not None
    assert counter.replay is provider
    assert await counter.count("hello world") == len("hello world")
    url, body, _headers = wire.requests[0]
    assert url == "https://api.openai.com/v1/responses/input_tokens"
    assert body["model"] == "gpt-6-astra"

    proxied = _openai_provider(
        tmp_path,
        wire,
        provider="openai",
        id="gpt-6-astra",
        api_key="test-key",
        extra_kwargs={"base_url": "https://proxy.example/v1"},
    )
    assert proxied.token_counter.count is None


@pytest.mark.asyncio
async def test_llama_cpp_counts_through_tokenize_with_the_configured_client(tmp_path: Path) -> None:
    wire = _Wire(lambda body: {"tokens": list(range(len(body["content"])))})
    provider = _openai_provider(
        tmp_path,
        wire,
        provider="llama_cpp",
        id="gpt-4o",
        api_key="local-key",
        extra_kwargs={"base_url": "http://llama.test:8080/v1"},
    )
    counter = provider.token_counter
    assert counter.count is not None
    assert not counter.exact_locally, "a llama.cpp server may serve any model under an OpenAI name"
    assert await counter.count("hello") == 5
    url, body, headers = wire.requests[0]
    assert url == "http://llama.test:8080/tokenize"
    assert body == {"content": "hello", "add_special": False}
    assert headers["authorization"] == "Bearer local-key"


@pytest.mark.asyncio
async def test_openai_models_on_openai_routes_count_locally(tmp_path: Path) -> None:
    load_o200k()
    wire = _Wire(lambda _body: {"input_tokens": 1})
    for model in (
        {"provider": "openai", "id": "gpt-4o", "api_key": "test-key"},
        {"provider": "openai", "id": "gpt-4o", "api": "chat_completions", "api_key": "test-key"},
        {"provider": "openrouter", "id": "openai/gpt-4.1", "api_key": "test-key"},
    ):
        counter = _openai_provider(tmp_path, wire, **model).token_counter
        assert counter.exact_locally, model
        assert (await count_exact(counter, "hello world")).kind == "exact"
    assert wire.requests == []
    other = _openai_provider(tmp_path, wire, provider="openrouter", id="qwen/qwen3.8-27b", api_key="k").token_counter
    assert not other.exact_locally
    assert other.count is None


def test_wrapped_provider_forwards_the_token_counter(tmp_path: Path) -> None:
    wire = _Wire(lambda _body: {"tokens": []})
    adapter = _openai_provider(tmp_path, wire, provider="openai", id="gpt-6-astra", api_key="test-key")
    wrapped = WrappedProvider(adapter, lambda inner, request: inner.stream(request))
    assert wrapped.token_counter.key == adapter.token_counter.key
    assert wrapped.token_counter.replay is adapter


class _CountResult:
    input_tokens = 3
    total_tokens = 3


@pytest.mark.asyncio
async def test_counting_builds_the_sdk_client_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building a client can discover Google credentials synchronously, which must not stall the event loop."""
    loop_thread = threading.get_ident()
    built_on: list[int] = []

    def client(*_args: object) -> object:
        built_on.append(threading.get_ident())
        return SimpleNamespace(
            messages=SimpleNamespace(count_tokens=AsyncMock(return_value=_CountResult())),
            aio=SimpleNamespace(models=SimpleNamespace(count_tokens=AsyncMock(return_value=_CountResult()))),
        )

    async with httpx2.AsyncClient() as http_client:
        claude = provider_for(ROUTE_CONFIGS["vertexai_claude"], tmp_path, http_client)
        monkeypatch.setattr(type(claude), "_client", client)
        await claude._count_message_tokens("hello")
    gemini = build_adapter({"provider": "google", "id": "gemini-3.8-flash", "api_key": "k"}, tmp_path, _Wire(dict))
    monkeypatch.setattr(type(gemini), "_sdk_client", client)
    await gemini._count_content_tokens("hello")

    assert len(built_on) == 2
    assert loop_thread not in built_on


def test_counter_keys_tell_the_routes_of_one_model_apart(tmp_path: Path) -> None:
    """A failing route pauses its own counting only, so routes serving one model never share a key."""
    direct = build_adapter({"provider": "google", "id": "gemini-3.8-flash", "api_key": "k"}, tmp_path, _Wire(dict))
    vertex = build_adapter(
        {
            "provider": "google",
            "id": "gemini-3.8-flash",
            "extra_kwargs": {"vertexai": True, "project_id": "one", "location": "us-central1"},
        },
        tmp_path,
        _Wire(dict),
    )
    other_project = build_adapter(
        {
            "provider": "google",
            "id": "gemini-3.8-flash",
            "extra_kwargs": {"vertexai": True, "project_id": "two", "location": "us-central1"},
        },
        tmp_path,
        _Wire(dict),
    )
    keys = {direct.token_counter.key, vertex.token_counter.key, other_project.token_counter.key}
    assert len(keys) == 3

    east = dict(ROUTE_CONFIGS["bedrock_claude"])
    west = {**east, "extra_kwargs": {**east["extra_kwargs"], "aws_region": "us-west-2"}}
    bedrock_keys = {provider_for(config, tmp_path, httpx2.AsyncClient()).token_counter.key for config in (east, west)}
    assert len(bedrock_keys) == 2
