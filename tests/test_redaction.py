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
