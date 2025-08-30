"""Tests for error handling module."""

from mindroom.error_handling import get_user_friendly_error_message


def test_api_key_error() -> None:
    """Test API key error message."""
    error = Exception("Invalid API key")
    message = get_user_friendly_error_message(error, "assistant")
    assert "[assistant]" in message
    assert "Authentication failed" in message


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
