"""Tests for error handling module."""

import httpx
from agno.exceptions import ModelProviderError
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


def test_provider_auth_error_redacts_secret_from_user_message() -> None:
    """Provider exception text should be redacted before Matrix-visible user output."""
    error = OpenAIAuthError(
        message="Incorrect API key provided: sk-test-secret",
        response=_MOCK_RESPONSE,
        body=None,
    )

    message = get_user_friendly_error_message(error, "assistant")

    assert "Authentication failed" in message
    assert "***redacted***" in message
    assert "sk-test-secret" not in message


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


def test_typed_rate_limit_error_uses_status_code() -> None:
    """Typed 429 errors remain rate limits even when their message is generic."""
    error = ModelProviderError(message="upstream unavailable", status_code=429)

    message = get_user_friendly_error_message(error)

    assert "Rate limited" in message
    assert "temporarily unavailable" not in message


def test_overloaded_provider_error_is_user_friendly() -> None:
    """An exhausted provider overload must not dump its raw payload to Matrix."""
    error = Exception(
        "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_secret'}",
    )

    message = get_user_friendly_error_message(error, "assistant")

    assert message == (
        "[assistant] ⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
    )
    assert "req_secret" not in message


def test_internal_provider_error_is_user_friendly() -> None:
    """A transient provider api_error gets the same bounded-retry message."""
    error = Exception(
        "{'type': 'error', 'error': {'type': 'api_error', 'message': 'Internal server error'}, "
        "'request_id': 'req_secret'}",
    )

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message
    assert "req_secret" not in message


def test_json_provider_error_is_case_insensitive() -> None:
    """Structured JSON provider payloads normalize error type and message casing."""
    error = Exception(
        '{"type":"error","error":{"type":"API_ERROR","message":"Internal Server Error"},"request_id":"req_secret"}',
    )

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message
    assert "req_secret" not in message


def test_typed_model_provider_error_is_user_friendly() -> None:
    """Typed provider errors use their status instead of message substrings."""
    error = ModelProviderError(message="upstream unavailable", status_code=503)

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message


def test_unstructured_overloaded_text_is_not_misclassified() -> None:
    """Arbitrary application errors containing overloaded retain useful details."""
    error = Exception("Local model overloaded while loading workspace state")

    message = get_user_friendly_error_message(error)

    assert message == "⚠️ Error: Local model overloaded while loading workspace state"


def test_unstructured_api_error_text_is_not_misclassified() -> None:
    """Provider-like words alone do not suppress an arbitrary application error."""
    error = Exception("api_error: Internal Server Error while reading local cache")

    message = get_user_friendly_error_message(error)

    assert message == "⚠️ Error: api_error: Internal Server Error while reading local cache"


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
