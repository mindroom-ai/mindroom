"""Tests for error handling module."""

import httpx
from anthropic import AuthenticationError as AnthropicAuthError
from openai import AuthenticationError as OpenAIAuthError

from mindroom.error_handling import _extract_provider_from_error, get_user_friendly_error_message

_MOCK_RESPONSE = httpx.Response(status_code=401, request=httpx.Request("POST", "https://api.example.com"))


def test_api_key_error() -> None:
    """Test API key error message includes the original error."""
    error = Exception("Invalid API key")
    message = get_user_friendly_error_message(error, "assistant")
    assert "[assistant]" in message
    assert "Authentication failed" in message
    assert "Invalid API key" in message


def test_api_key_error_with_provider() -> None:
    """Test that provider is extracted from exception module."""
    error = OpenAIAuthError(message="Incorrect API key provided", response=_MOCK_RESPONSE, body=None)
    message = get_user_friendly_error_message(error, "assistant")
    assert "(openai)" in message
    assert "Authentication failed" in message


def test_401_error() -> None:
    """Test that 401 errors are recognized as auth failures."""
    error = Exception("Error code: 401 - Unauthorized")
    message = get_user_friendly_error_message(error)
    assert "Authentication failed" in message


def test_generic_api_word_not_false_positive() -> None:
    """Test that the word 'api' alone does not trigger auth error."""
    error = Exception("Failed to connect to api endpoint")
    message = get_user_friendly_error_message(error)
    # Should NOT be auth error - just contains 'api' but no auth keywords
    assert "Authentication failed" not in message
    assert "Error:" in message


def test_rate_limit_error() -> None:
    """Test rate limit error message."""
    error = Exception("Rate limit exceeded")
    message = get_user_friendly_error_message(error)
    assert "Rate limited" in message


def test_timeout_error() -> None:
    """Test timeout error message."""
    error = TimeoutError("Request timeout")
    message = get_user_friendly_error_message(error, "bot")
    assert "[bot]" in message
    assert "timed out" in message


def test_generic_error() -> None:
    """Test generic error shows actual error message."""
    error = ValueError("Something went wrong")
    message = get_user_friendly_error_message(error)
    assert "Error: Something went wrong" in message


def test_extract_provider_from_error() -> None:
    """Test provider extraction from exception module."""
    openai_err = OpenAIAuthError(message="test", response=_MOCK_RESPONSE, body=None)
    assert _extract_provider_from_error(openai_err) == "openai"

    anthropic_err = AnthropicAuthError(message="test", response=_MOCK_RESPONSE, body=None)
    assert _extract_provider_from_error(anthropic_err) == "anthropic"

    assert _extract_provider_from_error(Exception("test")) is None
