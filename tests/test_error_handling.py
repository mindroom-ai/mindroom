"""Tests for error handling module."""

from mindroom.error_handling import (
    ErrorCategory,
    categorize_error,
    get_user_friendly_error_message,
)


class TestErrorCategorization:
    """Test error categorization logic."""

    def test_api_key_errors(self):
        """Test that API key errors are categorized correctly."""
        errors = [
            Exception("Invalid API key provided"),
            Exception("401 Unauthorized"),
            Exception("Authentication failed"),
            Exception("api_key is missing"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.API_KEY

    def test_rate_limit_errors(self):
        """Test that rate limit errors are categorized correctly."""
        errors = [
            Exception("Rate limit exceeded"),
            Exception("429 Too Many Requests"),
            Exception("Quota exceeded"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.RATE_LIMIT

    def test_network_errors(self):
        """Test that network errors are categorized correctly."""
        errors = [
            Exception("Connection refused"),
            Exception("Network unreachable"),
            Exception("SSL certificate verification failed"),
            Exception("DNS resolution failed"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.NETWORK

    def test_timeout_errors(self):
        """Test that timeout errors are categorized correctly."""
        errors = [
            Exception("Request timeout"),
            TimeoutError("Operation timed out"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.TIMEOUT

    def test_permission_errors(self):
        """Test that permission errors are categorized correctly."""
        errors = [
            Exception("Permission denied"),
            Exception("403 Forbidden"),
            Exception("Access denied to resource"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.PERMISSION

    def test_unknown_errors(self):
        """Test that unknown errors are categorized as unknown."""
        errors = [
            Exception("Some random error"),
            ValueError("Invalid value"),
            RuntimeError("Runtime problem"),
        ]

        for error in errors:
            assert categorize_error(error) == ErrorCategory.UNKNOWN


class TestUserFriendlyMessages:
    """Test user-friendly error message generation."""

    def test_api_key_message(self):
        """Test API key error message generation."""
        error = Exception("Invalid OpenAI API key")
        message = get_user_friendly_error_message(error, "assistant")

        assert "[assistant]" in message
        assert "Authentication Error" in message
        assert "API key" in message
        assert "OpenAI" in message

    def test_rate_limit_message(self):
        """Test rate limit error message generation."""
        error = Exception("Rate limit exceeded")
        message = get_user_friendly_error_message(error, "researcher")

        assert "[researcher]" in message
        assert "Rate Limit" in message
        assert "wait" in message

    def test_network_message(self):
        """Test network error message generation."""
        error = Exception("Connection timeout")
        message = get_user_friendly_error_message(error)

        assert "Network Error" in message or "Timeout" in message
        assert "try again" in message

    def test_provider_extraction(self):
        """Test that provider names are extracted correctly."""
        test_cases = [
            ("OpenAI API key invalid", "OpenAI"),
            ("Anthropic API authentication failed", "Anthropic"),
            ("Claude API key error", "Anthropic"),
            ("Google Gemini authentication error", "Google Gemini"),
            ("Ollama API key missing", "Ollama"),
        ]

        for error_text, expected_provider in test_cases:
            error = Exception(error_text)
            message = get_user_friendly_error_message(error)

            # These should all be API key errors, so check for both provider and auth error
            assert "Authentication Error" in message
            assert expected_provider in message or "AI service" in message

    def test_message_without_agent_name(self):
        """Test message generation without agent name."""
        error = Exception("API key invalid")
        message = get_user_friendly_error_message(error)

        assert "[" not in message  # No agent prefix
        assert "Authentication Error" in message

    def test_tool_error_message(self):
        """Test tool error message generation."""
        error = Exception("Tool 'search' execution failed")
        message = get_user_friendly_error_message(error, "analyst")

        assert "[analyst]" in message
        assert "Tool Error" in message
        assert "search" in message or "tool" in message
