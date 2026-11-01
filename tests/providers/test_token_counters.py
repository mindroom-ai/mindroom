"""Each provider route's token counter: OpenAI's own tokenizer where the route serves OpenAI's models, else an estimate."""
# ruff: noqa: D103

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.token_counting import families
from mindroom.token_counting.counts import count
from mindroom.token_counting.families import load_o200k
from tests.providers.anthropic_family_support import ROUTE_CONFIGS, provider_for
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.openai_family_support import provider_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider


class _OneTokenPerCharacter:
    def encode(self, text: str, *, disallowed_special: tuple[str, ...]) -> list[str]:  # noqa: ARG002
        return list(text)


def _o200k(text: str) -> int:
    return len(load_o200k().encode(text, disallowed_special=()))


def _openai_provider(tmp_path: Path, **model: Any) -> ModelProvider:  # noqa: ANN401
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(500)))
    return build_openai_provider(ModelConfig(**model), provider_runtime(runtime_paths, http_client=http_client))


@pytest.mark.parametrize(
    ("model", "own_tokenizer"),
    [
        ({"provider": "openai", "id": "gpt-4", "api_key": "k"}, True),
        ({"provider": "openai", "id": "gpt-4", "api": "chat_completions", "api_key": "k"}, True),
        ({"provider": "openrouter", "id": "openai/gpt-4", "api_key": "k"}, True),
        (
            {"provider": "openai", "id": "gpt-4", "api_key": "k", "extra_kwargs": {"base_url": "https://p.example/v1"}},
            False,
        ),
        ({"provider": "openrouter", "id": "qwen/gpt-4", "api_key": "k"}, False),
        ({"provider": "llama_cpp", "id": "gpt-4", "extra_kwargs": {"base_url": "http://llama.test:8080/v1"}}, False),
    ],
)
def test_only_routes_serving_openais_own_models_count_with_their_tokenizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: dict[str, Any],
    own_tokenizer: bool,
) -> None:
    """A proxy or local server may serve any model under an OpenAI name, so the name only picks its estimate there."""
    monkeypatch.setitem(families._encoders, "cl100k_base", families._Encoder(value=_OneTokenPerCharacter()))
    counter = _openai_provider(tmp_path, **model).token_counter
    assert count(counter, "hello world") == (11 if own_tokenizer else _o200k("hello world"))


def test_claude_and_gemini_routes_estimate_with_their_family_margin(tmp_path: Path) -> None:
    text = "A sentence with numbers 2026-11-01 and `code()`.\n" * 10
    claude = provider_for(ROUTE_CONFIGS["anthropic"], tmp_path, httpx2.AsyncClient())
    gemini = build_adapter({"provider": "google", "id": "gemini-3.8-flash", "api_key": "k"}, tmp_path, lambda _r: None)
    assert count(claude.token_counter, text) == math.ceil(_o200k(text) * 1.8)
    assert count(gemini.token_counter, text) == math.ceil(_o200k(text) * 1.35)


def test_routes_that_know_their_family_estimate_with_its_margin_whatever_the_model_id(tmp_path: Path) -> None:
    """A Bedrock inference profile ARN or a tuned Gemini model's id need not name the family."""
    text = "A sentence with numbers 2026-11-01 and `code()`.\n" * 10
    profile = {
        **ROUTE_CONFIGS["bedrock_claude"],
        "id": "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2",
    }
    claude = provider_for(profile, tmp_path, httpx2.AsyncClient())
    gemini = build_adapter(
        {"provider": "google", "id": "tunedModels/support-bot", "api_key": "k"},
        tmp_path,
        lambda _r: None,
    )
    assert count(claude.token_counter, text) == math.ceil(_o200k(text) * 1.8)
    assert count(gemini.token_counter, text) == math.ceil(_o200k(text) * 1.35)


def test_wrapped_provider_forwards_the_token_counter(tmp_path: Path) -> None:
    adapter = _openai_provider(tmp_path, provider="openai", id="gpt-6-astra", api_key="test-key")
    wrapped = WrappedProvider(adapter, lambda inner, request: inner.stream(request))
    assert count(wrapped.token_counter, "hello world") == count(adapter.token_counter, "hello world")
    assert wrapped.token_counter.replay is adapter
