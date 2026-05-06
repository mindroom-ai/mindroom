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
