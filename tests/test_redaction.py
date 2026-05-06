"""Tests for centralized credential redaction helpers."""

from __future__ import annotations

from mindroom.redaction import REDACTED, redact_sensitive_data


def test_redact_sensitive_data_redacts_nested_dicts_lists_and_header_variants() -> None:
    """Nested values and case-insensitive header spellings should be redacted."""
    payload = {
        "headers": {
            "Authorization": "Bearer auth-secret",
            "COOKIE": "session=secret",
            "set-cookie": "session=secret",
            "X-Api-Key": "api-secret",
        },
        "tokens": [
            {"access_token": "access-secret"},
            {"refreshToken": "refresh-secret"},
            {"id-token": "id-secret"},
            {"client_secret": "client-secret"},
        ],
        "safe": {"name": "kept"},
    }

    assert redact_sensitive_data(payload) == {
        "headers": {
            "Authorization": REDACTED,
            "COOKIE": REDACTED,
            "set-cookie": REDACTED,
            "X-Api-Key": REDACTED,
        },
        "tokens": [
            {"access_token": REDACTED},
            {"refreshToken": REDACTED},
            {"id-token": REDACTED},
            {"client_secret": REDACTED},
        ],
        "safe": {"name": "kept"},
    }


def test_redact_sensitive_data_redacts_oauth_callback_query_values_in_urls() -> None:
    """OAuth callback codes and state values should not survive inside logged URLs."""
    redacted = redact_sensitive_data(
        {
            "url": "https://example.test/api/oauth/google/callback?code=code-secret&state=state-secret&keep=1",
            "query_params": {"code": "code-secret", "state": "state-secret", "keep": "1"},
        },
    )

    assert redacted == {
        "url": "https://example.test/api/oauth/google/callback?code=***redacted***&state=***redacted***&keep=1",
        "query_params": {"code": REDACTED, "state": REDACTED, "keep": "1"},
    }


def test_redact_sensitive_data_redacts_secret_assignments_inside_embedded_text_values() -> None:
    """Non-secret wrapper fields should not hide secret-looking text inside their values."""
    redacted = redact_sensitive_data(
        {
            "payload": '{"password":"pw-secret"}',
            "error": '{"api_key":"api-secret"}',
            "metadata": "token=tok-secret",
        },
    )

    assert redacted == {
        "payload": '{"password":"***redacted***"}',
        "error": '{"api_key":"***redacted***"}',
        "metadata": "token=***redacted***",
    }


def test_redact_sensitive_data_does_not_truncate_by_default() -> None:
    """Redaction should not drop non-secret debug data unless a caller asks for bounds."""
    long_text = "x" * 5000

    assert redact_sensitive_data({"message": long_text}) == {"message": long_text}


def test_redact_sensitive_data_supports_explicit_bounds_for_durable_tool_logs() -> None:
    """Callers with durable size budgets can opt into truncation separately from redaction."""
    redacted = redact_sensitive_data(
        {"message": "x" * 100, "items": [str(index) for index in range(4)]},
        max_string_length=20,
        max_collection_items=2,
        max_depth=6,
    )

    assert redacted == {
        "message": "xxxxx... [truncated]",
        "items": ["0", "1", "... [truncated]"],
    }
