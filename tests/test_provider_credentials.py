"""Model config validation and the checks that report a model without a usable key."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.providers.credentials import missing_model_api_key_provider
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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
            "unkeyed": ModelConfig(provider="openai", id="gpt-6-astra"),
        },
    )

    assert [name for name in config.models if missing_model_api_key_provider(config, runtime_paths, name)] == [
        "unkeyed",
    ]


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
