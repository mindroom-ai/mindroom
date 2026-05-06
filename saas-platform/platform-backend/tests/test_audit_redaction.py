"""Regression tests for audit-log credential redaction."""

from __future__ import annotations

from backend.utils.audit import REDACTED, redact_audit_details


def test_redact_audit_details_recurses_and_matches_case_insensitive_headers() -> None:
    """Audit details should keep shape while masking nested bearer material."""
    details = {
        "headers": {
            "Authorization": "Bearer auth-secret",
            "COOKIE": "session=secret",
            "set-cookie": "session=secret",
        },
        "body": {
            "access_token": "access-secret",
            "nested": [{"clientSecret": "client-secret"}, {"safe": "kept"}],
        },
    }

    assert redact_audit_details(details) == {
        "headers": {
            "Authorization": REDACTED,
            "COOKIE": REDACTED,
            "set-cookie": REDACTED,
        },
        "body": {
            "access_token": REDACTED,
            "nested": [{"clientSecret": REDACTED}, {"safe": "kept"}],
        },
    }


def test_redact_audit_details_redacts_free_form_secret_strings() -> None:
    """Audit text fields should not leak bearer material under ordinary keys."""
    details = {
        "message": "Authorization: Bearer auth-secret",
        "error": "api_key=api-secret",
        "nested": ["password=pw-secret", {"note": "client_secret=client-secret"}],
    }

    assert redact_audit_details(details) == {
        "message": f"Authorization: Bearer {REDACTED}",
        "error": f"api_key={REDACTED}",
        "nested": [f"password={REDACTED}", {"note": f"client_secret={REDACTED}"}],
    }


def test_redact_audit_details_redacts_oauth_url_and_query_values() -> None:
    """OAuth callback codes and states should be masked in URLs and query containers."""
    details = {
        "callback_url": "https://example.test/cb?code=code-secret&state=state-secret&keep=1",
        "signed_url": "https://user:pass-secret@example.test/file?signature=sig-secret&name=file",
        "query_params": {"code": "code-secret", "state": "state-secret", "keep": "1"},
    }

    assert redact_audit_details(details) == {
        "callback_url": f"https://example.test/cb?code={REDACTED}&state={REDACTED}&keep=1",
        "signed_url": f"https://user:***@example.test/file?signature={REDACTED}&name=file",
        "query_params": {"code": REDACTED, "state": REDACTED, "keep": "1"},
    }
