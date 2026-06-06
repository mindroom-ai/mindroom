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
            {"apiToken": "api-token-secret"},
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
            {"apiToken": REDACTED},
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


def test_redact_sensitive_data_redacts_bare_query_fragments_under_query_keys() -> None:
    """Raw callback query strings should be redacted when logged as structured fields."""
    redacted = redact_sensitive_data(
        {
            "query_string": "code=code-secret&state=state-secret&keep=1",
            "callback_query": "x_goog_signature=sig-secret&name=file",
            "nested": {"query_params": "access_token=access-secret&keep=1"},
        },
    )

    assert redacted == {
        "query_string": f"code={REDACTED}&state={REDACTED}&keep=1",
        "callback_query": f"x_goog_signature={REDACTED}&name=file",
        "nested": {"query_params": f"access_token={REDACTED}&keep=1"},
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


def test_redact_sensitive_data_redacts_secret_before_truncated_bound() -> None:
    """Bounded redaction should keep scanning far enough to redact text that can survive truncation."""
    redacted = redact_sensitive_data(
        {"message": "x" * 50 + " api_key=sk-test-secret " + "y" * 5000},
        max_string_length=120,
    )

    message = redacted["message"]
    assert isinstance(message, str)
    assert REDACTED in message
    assert "sk-test-secret" not in message
    assert len(message) <= 120


def test_redact_sensitive_data_tolerates_malformed_ipv6_url() -> None:
    """A URL-like token with an unbalanced IPv6 bracket must not crash redaction (ISSUE-230)."""
    redacted = redact_sensitive_data({"message": 'see <a href="http://[">x</a> for details'})

    message = redacted["message"]
    assert isinstance(message, str)
    assert "http://[" in message


def test_redact_sensitive_data_uses_context_for_bare_values_in_secret_lists() -> None:
    """List items under a secret-bearing key should be redacted without changing container shape."""
    redacted = redact_sensitive_data(
        {
            "api_keys": ["plain-secret-one", "plain-secret-two"],
            "oauth_tokens": ["plain-oauth-token"],
            "max_tokens": 4096,
            "next_token": "cursor-value",
            "usage": {
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "input_tokens": 4,
                "output_tokens": 5,
            },
            "nested": {"tokens": [{"value": "plain-token"}]},
            "safe_values": ["plain-secret-one"],
        },
    )

    assert redacted == {
        "api_keys": [REDACTED, REDACTED],
        "oauth_tokens": [REDACTED],
        "max_tokens": 4096,
        "next_token": "cursor-value",
        "usage": {
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "input_tokens": 4,
            "output_tokens": 5,
        },
        "nested": {"tokens": [{"value": REDACTED}]},
        "safe_values": ["plain-secret-one"],
    }


def test_redact_sensitive_data_redacts_value_fields_named_by_sibling_secret_keys() -> None:
    """Key/value style containers should redact bare values when the sibling name is secret-like."""
    redacted = redact_sensitive_data(
        {
            "environment": [
                {"name": "OPENAI_API_KEY", "value": "plain-openai-secret"},
                {"key": "client_secret", "value": "plain-client-secret"},
                {"name": "mode", "value": "safe"},
            ],
            "headers": [{"name": "Authorization", "value": "plain-auth-secret"}],
        },
    )

    assert redacted == {
        "environment": [
            {"name": "OPENAI_API_KEY", "value": REDACTED},
            {"key": "client_secret", "value": REDACTED},
            {"name": "mode", "value": "safe"},
        ],
        "headers": [{"name": "Authorization", "value": REDACTED}],
    }


def test_redact_sensitive_data_keeps_values_for_non_schema_label_keys() -> None:
    """Field/parameter/variable labels should not force-redact harmless values."""
    redacted = redact_sensitive_data(
        [
            {"field": "password_policy", "value": "min length 12"},
            {"parameter": "client_secret_required", "value": False},
            {"variable": "secret_sauce_recipe", "value": "tomatoes"},
        ],
    )

    assert redacted == [
        {"field": "password_policy", "value": "min length 12"},
        {"parameter": "client_secret_required", "value": False},
        {"variable": "secret_sauce_recipe", "value": "tomatoes"},
    ]
