"""Model config validation and the checks that report a model without a usable key."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.cli.config import find_missing_env_variables
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.llm.errors import AuthenticationFailed
from mindroom.llm.provider import ModelRequest
from mindroom.providers.credentials import missing_model_api_key_provider, own_api_key, unset_api_key_variable
from mindroom.providers.registry import build_model_provider, configured_model
from mindroom.topic_generator import generate_room_topic_ai
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.mark.parametrize(("provider", "api"), [("openai", "invalid"), ("anthropic", "responses")])
def test_model_api_rejects_invalid_values_and_unsupported_providers(provider: str, api: str) -> None:
    """A transport selection must not be silently ignored or misspelled."""
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({"provider": provider, "id": "test-model", "api": api})


def test_missing_model_api_key_provider_honors_model_credentials(tmp_path: Path) -> None:
    """First-run provider setup must not ask for a shared key a model does not use."""
    runtime_paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials("model:dashboard", {"api_key": "sk-ui"})
    config = Config(
        models={
            "keyed": ModelConfig(provider="openai", id="gpt-6-astra", api_key="sk-config"),
            "dashboard": ModelConfig(provider="openai", id="gpt-6-astra"),
            "token": ModelConfig(provider="anthropic", id="claude-sonnet-5", extra_kwargs={"auth_token": "tok"}),
            "client": ModelConfig(
                provider="openai",
                id="gpt-6-astra",
                extra_kwargs={"client_params": {"api_key": "sk-client"}},
            ),
            "unkeyed": ModelConfig(provider="openai", id="gpt-6-astra"),
        },
    )

    assert [name for name in config.models if missing_model_api_key_provider(config, runtime_paths, name)] == [
        "unkeyed",
    ]


def _keyed_models(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    (tmp_path / ".env").write_text("OPENROUTER_TEAM_API_KEY=sk-team\n", encoding="utf-8")
    runtime_paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials("model:dashboard", {"api_key": "sk-ui"})
    team, unset = "${OPENROUTER_TEAM_API_KEY}", "${OPENROUTER_OTHER_API_KEY}"
    client = {"client_params": {"api_key": team}}
    models = {
        "default": ModelConfig(provider="openrouter", id="m", api_key=unset),
        "field": ModelConfig(provider="openrouter", id="m", api_key=team),
        "extra": ModelConfig(provider="openrouter", id="m", extra_kwargs={"api_key": team}),
        "client": ModelConfig(provider="openai", id="m", api_key=unset, extra_kwargs=client),
        "partial": ModelConfig(provider="openrouter", id="m", api_key=f"prefix-{team}"),
        "dashboard": ModelConfig(provider="openrouter", id="m", extra_kwargs={"api_key": unset}),
        "keyless": ModelConfig(provider="codex", id="m", api_key=unset),
        "ollama": ModelConfig(provider="ollama", id="m", api_key=unset),
    }
    return Config(models=models), runtime_paths


def test_a_model_key_written_as_an_env_reference_resolves_from_the_runtime_env(tmp_path: Path) -> None:
    """Only the whole-value ``${NAME}`` key the route sends is read from `.env`; a key that loses is left alone."""
    config, runtime_paths = _keyed_models(tmp_path)

    assert own_api_key(configured_model(config, runtime_paths, "field")) == "sk-team"
    assert own_api_key(configured_model(config, runtime_paths, "extra")) == "sk-team"
    assert own_api_key(configured_model(config, runtime_paths, "client")) == "sk-team"
    assert own_api_key(configured_model(config, runtime_paths, "dashboard")) == "sk-ui"
    assert own_api_key(configured_model(config, runtime_paths, "partial")) == "prefix-${OPENROUTER_TEAM_API_KEY}"
    for name in ("field", "extra", "client", "partial", "dashboard", "keyless"):
        assert unset_api_key_variable(configured_model(config, runtime_paths, name), runtime_paths) is None
    assert find_missing_env_variables(config, runtime_paths) == [
        ("ollama", "OPENROUTER_OTHER_API_KEY", ("ollama",)),
        ("openrouter", "OPENROUTER_OTHER_API_KEY", ("default",)),
    ]


@pytest.mark.asyncio
async def test_an_unset_key_reference_fails_the_request_and_not_the_build(tmp_path: Path) -> None:
    """A model whose variable is unset still builds, and its request fails naming the variable; a room gets no topic."""
    config, runtime_paths = _keyed_models(tmp_path)

    configured_model(config, runtime_paths, "ollama")
    provider = build_model_provider(config, runtime_paths, "default")
    with pytest.raises(AuthenticationFailed, match="OPENROUTER_OTHER_API_KEY not set"):
        await anext(aiter(provider.stream(ModelRequest(messages=()))))
    assert await generate_room_topic_ai("lobby", "Lobby", config, runtime_paths) is None


def test_a_gemini_model_on_vertex_ai_needs_no_google_key(tmp_path: Path) -> None:
    """``GOOGLE_GENAI_USE_VERTEXAI`` in ``.env`` moves every Gemini model to Vertex AI, which authenticates without one."""
    config = Config(models={"default": ModelConfig(provider="gemini", id="gemini-3.8-flash")})
    gemini_api = test_runtime_paths(tmp_path / "gemini-api")
    (tmp_path / "vertex").mkdir()
    (tmp_path / "vertex" / ".env").write_text("GOOGLE_GENAI_USE_VERTEXAI=true\n", encoding="utf-8")
    vertex = test_runtime_paths(tmp_path / "vertex")

    assert missing_model_api_key_provider(config, gemini_api, "default") == "google"
    assert missing_model_api_key_provider(config, vertex, "default") is None
    enterprise = ModelConfig(
        provider="gemini",
        id="gemini-3.8-flash",
        extra_kwargs={"client_params": {"enterprise": True}},
    )
    assert missing_model_api_key_provider(Config(models={"default": enterprise}), gemini_api, "default") is None


def test_model_config_rejects_api_key_in_both_fields_without_echoing_keys() -> None:
    """Two configured keys would leave one unused, and the error must not print either."""
    model = {"provider": "openai", "id": "model", "api_key": "sk-secret1", "extra_kwargs": {"api_key": "sk-secret2"}}
    with pytest.raises(ValidationError, match=r"either api_key or extra_kwargs\.api_key") as exc_info:
        Config.model_validate({"models": {"default": model}})

    assert "sk-secret" not in str(exc_info.value)


@pytest.mark.parametrize("api_key", [123, True, ["sk-a"]])
def test_model_config_rejects_non_string_extra_kwargs_api_key(api_key: object) -> None:
    """A non-string key (for example YAML ``yes``) must fail validation instead of reaching the provider."""
    with pytest.raises(ValidationError, match=r"extra_kwargs\.api_key must be a string"):
        ModelConfig(provider="openai", id="gpt-6-astra", extra_kwargs={"api_key": api_key})
