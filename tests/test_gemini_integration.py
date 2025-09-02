"""Tests for Google Gemini integration."""

from unittest.mock import MagicMock, patch

import pytest

from src.mindroom.ai import get_model_instance
from src.mindroom.config import Config


class TestGeminiIntegration:
    """Test Google Gemini model integration."""

    def test_gemini_provider_creates_gemini_instance(self) -> None:
        """Test that 'gemini' provider creates a Gemini instance."""
        config = Config()
        config.models = {
            "test_model": MagicMock(
                provider="gemini",
                id="gemini-2.0-flash-001",
                host=None,
            ),
        }

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}):
            model = get_model_instance(config, "test_model")
            assert model.__class__.__name__ == "Gemini"
            assert model.id == "gemini-2.0-flash-001"
            assert model.provider == "Google"

    def test_google_provider_creates_gemini_instance(self) -> None:
        """Test that 'google' provider also creates a Gemini instance."""
        config = Config()
        config.models = {
            "test_model": MagicMock(
                provider="google",
                id="gemini-2.0-pro-001",
                host=None,
            ),
        }

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}):
            model = get_model_instance(config, "test_model")
            assert model.__class__.__name__ == "Gemini"
            assert model.id == "gemini-2.0-pro-001"
            assert model.provider == "Google"

    def test_gemini_api_key_environment_variable(self) -> None:
        """Test that GOOGLE_API_KEY is set from credentials manager."""
        config = Config()
        config.models = {
            "test_model": MagicMock(
                provider="gemini",
                id="gemini-2.0-flash-001",
                host=None,
            ),
        }

        with patch("src.mindroom.ai.get_api_key_for_provider") as mock_get_api_key:
            mock_get_api_key.return_value = "test-google-api-key"
            with patch.dict("os.environ", {}, clear=True):
                get_model_instance(config, "test_model")
                # Check that the API key was retrieved for gemini
                mock_get_api_key.assert_called_with("gemini")

    def test_unsupported_provider_raises_error(self) -> None:
        """Test that unsupported providers raise appropriate errors."""
        config = Config()
        config.models = {
            "test_model": MagicMock(
                provider="unsupported_provider",
                id="some-model",
                host=None,
            ),
        }

        with pytest.raises(ValueError, match="Unsupported AI provider: unsupported_provider"):
            get_model_instance(config, "test_model")

    def test_gemini_models_in_config(self) -> None:
        """Test that Gemini models can be configured properly."""
        config = Config()

        # Test various Gemini model configurations
        gemini_configs = [
            ("gemini", "gemini-2.0-flash-001"),
            ("gemini", "gemini-2.0-pro-001"),
            ("google", "gemini-2.5-flash"),
            ("google", "gemini-1.5-pro-latest"),
        ]

        for provider, model_id in gemini_configs:
            config.models = {
                "test": MagicMock(
                    provider=provider,
                    id=model_id,
                    host=None,
                ),
            }

            with patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}):
                model = get_model_instance(config, "test")
                assert model.__class__.__name__ == "Gemini"
                assert model.id == model_id
                assert model.provider == "Google"
