"""Tests for Google Gemini integration."""

import tempfile
from pathlib import Path

import pytest

from mindroom.ai import get_model_instance
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_shared_credentials_manager


def _config_with_runtime_paths() -> tuple[Config, RuntimePaths]:
    runtime_root = Path(tempfile.mkdtemp())
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    return (
        Config.validate_with_runtime(
            {
                "connections": {
                    "google/default": {
                        "provider": "google",
                        "service": "google_gemini",
                        "auth_kind": "api_key",
                    },
                },
            },
            runtime_paths,
        ),
        runtime_paths,
    )


class TestGeminiIntegration:
    """Test Google Gemini model integration."""

    @staticmethod
    def _set_google_api_key(runtime_paths: RuntimePaths) -> None:
        get_runtime_shared_credentials_manager(runtime_paths).set_api_key("google_gemini", "test-google-api-key")

    def test_gemini_provider_creates_gemini_instance(self) -> None:
        """Test that 'gemini' provider creates a Gemini instance."""
        config, runtime_paths = _config_with_runtime_paths()
        self._set_google_api_key(runtime_paths)
        config.models = {
            "test_model": ModelConfig(
                provider="gemini",
                id="gemini-2.0-flash-001",
            ),
        }

        model = get_model_instance(config, runtime_paths, "test_model")
        assert model.__class__.__name__ == "Gemini"
        assert model.id == "gemini-2.0-flash-001"
        assert model.provider == "Google"

    def test_google_provider_creates_gemini_instance(self) -> None:
        """Test that 'google' provider also creates a Gemini instance."""
        config, runtime_paths = _config_with_runtime_paths()
        self._set_google_api_key(runtime_paths)
        config.models = {
            "test_model": ModelConfig(
                provider="google",
                id="gemini-2.0-pro-001",
            ),
        }

        model = get_model_instance(config, runtime_paths, "test_model")
        assert model.__class__.__name__ == "Gemini"
        assert model.id == "gemini-2.0-pro-001"
        assert model.provider == "Google"

    def test_gemini_uses_named_google_connection(self) -> None:
        """Gemini should load auth from the shared google connection, not process env."""
        config, runtime_paths = _config_with_runtime_paths()
        self._set_google_api_key(runtime_paths)
        config.models = {
            "test_model": ModelConfig(
                provider="gemini",
                id="gemini-2.0-flash-001",
            ),
        }

        model = get_model_instance(config, runtime_paths, "test_model")
        assert model.__class__.__name__ == "Gemini"
        assert getattr(model, "api_key", None) == "test-google-api-key"

    def test_unsupported_provider_raises_error(self) -> None:
        """Test that unsupported providers raise appropriate errors."""
        config, runtime_paths = _config_with_runtime_paths()
        config.models = {
            "test_model": ModelConfig(
                provider="unsupported_provider",
                id="some-model",
            ),
        }

        with pytest.raises(ValueError, match="Unsupported AI provider: unsupported_provider"):
            get_model_instance(config, runtime_paths, "test_model")

    def test_gemini_models_in_config(self) -> None:
        """Test that Gemini models can be configured properly."""
        config, runtime_paths = _config_with_runtime_paths()

        # Test various Gemini model configurations
        gemini_configs = [
            ("gemini", "gemini-2.0-flash-001"),
            ("gemini", "gemini-2.0-pro-001"),
            ("google", "gemini-2.5-flash"),
            ("google", "gemini-1.5-pro-latest"),
        ]

        for provider, model_id in gemini_configs:
            config.models = {
                "test": ModelConfig(
                    provider=provider,
                    id=model_id,
                ),
            }

            self._set_google_api_key(runtime_paths)
            model = get_model_instance(config, runtime_paths, "test")
            assert model.__class__.__name__ == "Gemini"
            assert model.id == model_id
            assert model.provider == "Google"
