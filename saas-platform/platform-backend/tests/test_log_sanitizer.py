"""Test log sanitization functionality."""

import os
from unittest.mock import patch


from backend.utils.log_sanitizer import sanitize_string as sanitize_message


class TestLogSanitizer:
    """Test log sanitization functionality."""

    def test_sanitize_uuid(self):
        """Test UUID redaction."""
        msg = "User 123e4567-e89b-12d3-a456-426614174000 accessed resource"
        sanitized = sanitize_message(msg)
        # In development, no sanitization
        assert "123e4567-e89b-12d3-a456-426614174000" in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_uuid_production(self):
        """Test UUID redaction in production."""
        msg = "User 123e4567-e89b-12d3-a456-426614174000 accessed resource"
        sanitized = sanitize_message(msg)
        assert "[UUID]" in sanitized
        assert "123e4567-e89b-12d3-a456-426614174000" not in sanitized

    def test_sanitize_email(self):
        """Test email redaction."""
        msg = "Login attempt from user@example.com failed"
        sanitized = sanitize_message(msg)
        # In development, no sanitization
        assert "user@example.com" in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_email_production(self):
        """Test email redaction in production."""
        msg = "Login attempt from user@example.com failed"
        sanitized = sanitize_message(msg)
        assert "[EMAIL]" in sanitized
        assert "user@example.com" not in sanitized

    def test_sanitize_bearer_token(self):
        """Test Bearer token redaction."""
        msg = "Auth header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz"
        sanitized = sanitize_message(msg)
        # In development, no sanitization
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz" in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_bearer_token_production(self):
        """Test Bearer token redaction in production."""
        msg = "Auth header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz"
        sanitized = sanitize_message(msg)
        assert "Bearer [TOKEN]" in sanitized
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in sanitized

    def test_sanitize_api_key(self):
        """Test API key redaction."""
        msg = 'Config: api_key="sk-proj-abcd1234efgh5678ijkl"'
        sanitized = sanitize_message(msg)
        # In development, no sanitization
        assert "sk-proj-abcd1234efgh5678ijkl" in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_api_key_production(self):
        """Test API key redaction in production."""
        msg = 'Config: api_key="sk-proj-abcd1234efgh5678ijkl"'
        sanitized = sanitize_message(msg)
        assert "[REDACTED]" in sanitized
        assert "sk-proj-abcd1234efgh5678ijkl" not in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_multiple_patterns(self):
        """Test multiple pattern redaction."""
        msg = (
            "User test@email.com with id 123e4567-e89b-12d3-a456-426614174000 "
            "used Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 to access API"
        )
        sanitized = sanitize_message(msg)

        assert "[EMAIL]" in sanitized
        assert "[UUID]" in sanitized
        assert "Bearer [TOKEN]" in sanitized
        assert "test@email.com" not in sanitized
        assert "123e4567-e89b-12d3-a456-426614174000" not in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_sanitize_password_variations(self):
        """Test various password/secret patterns."""
        patterns = [
            ('password="secret1234567890abcdefghij"', "[REDACTED]"),  # 20+ chars
            ("secret: abcd1234efgh5678ijkl9012mnop", "[REDACTED]"),
            ("api-key=sk-1234567890abcdefghij", "[REDACTED]"),
            ("token:Bearer1234567890abcdefghij", "[REDACTED]"),
        ]

        for original, expected in patterns:
            sanitized = sanitize_message(original)
            assert expected in sanitized

    def test_no_sanitization_in_development(self):
        """Test that development mode doesn't sanitize."""
        # Ensure we're in development (default)
        if "ENVIRONMENT" in os.environ:
            del os.environ["ENVIRONMENT"]

        sensitive_data = [
            "user@example.com",
            "123e4567-e89b-12d3-a456-426614174000",
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "api_key=sk-1234567890",
        ]

        for data in sensitive_data:
            msg = f"Log message with {data}"
            sanitized = sanitize_message(msg)
            assert data in sanitized  # Should not be redacted

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_preserve_non_sensitive_data(self):
        """Test that non-sensitive data is preserved."""
        msg = "Normal log message with numbers 12345 and words"
        sanitized = sanitize_message(msg)

        # Should not change non-sensitive content
        assert "Normal log message" in sanitized
        assert "12345" in sanitized
        assert "words" in sanitized

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_edge_cases(self):
        """Test edge cases."""
        # Empty string
        assert sanitize_message("") == ""

        # None (though shouldn't happen)
        assert sanitize_message(None) is None

        # Very long string
        long_msg = "x" * 10000
        assert sanitize_message(long_msg) == long_msg

    @patch.dict(os.environ, {"ENVIRONMENT": "production"})
    def test_case_insensitive_patterns(self):
        """Test case-insensitive pattern matching."""
        msg = "Bearer ABC123 and BEARER xyz789 and bearer def456"
        sanitized = sanitize_message(msg)

        # All Bearer variations should be redacted
        assert sanitized.count("Bearer [TOKEN]") == 3
        assert "ABC123" not in sanitized
        assert "xyz789" not in sanitized
        assert "def456" not in sanitized
