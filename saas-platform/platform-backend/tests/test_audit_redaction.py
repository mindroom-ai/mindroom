"""Regression tests for audit-log credential redaction."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from backend.middleware import audit_logging
from backend.middleware.audit_logging import AuditLoggingMiddleware
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


def test_redact_audit_details_redacts_bare_provider_token_formats() -> None:
    """Common provider token shapes should be masked even under ordinary keys."""
    details = {
        "openai": "sk_live_secret",
        "github": "ghp_secret",
        "github_pat": "github_pat_secret",
        "google": "AIzaSySecret",
        "slack": "xoxb-secret",
    }

    assert redact_audit_details(details) == {
        "openai": REDACTED,
        "github": REDACTED,
        "github_pat": REDACTED,
        "google": REDACTED,
        "slack": REDACTED,
    }


def test_redact_audit_details_redacts_oauth_url_and_query_values() -> None:
    """OAuth callback codes and states should be masked in URLs and query containers."""
    details = {
        "callback_url": "https://example.test/cb?code=code-secret&state=state-secret&keep=1",
        "signed_url": "https://user:pass-secret@example.test/file?signature=sig-secret&name=file",
        "query_params": {"code": "code-secret", "state": "state-secret", "keep": "1"},
        "query_string": "code=code-secret&state=state-secret&keep=1",
    }

    assert redact_audit_details(details) == {
        "callback_url": f"https://example.test/cb?code={REDACTED}&state={REDACTED}&keep=1",
        "signed_url": f"https://user:***@example.test/file?signature={REDACTED}&name=file",
        "query_params": {"code": REDACTED, "state": REDACTED, "keep": "1"},
        "query_string": f"code={REDACTED}&state={REDACTED}&keep=1",
    }


@pytest.mark.asyncio
async def test_audit_log_persists_non_object_json_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Array or scalar JSON bodies should keep audit rows instead of failing insertion."""
    table = Mock()
    table.insert.return_value.execute.return_value = Mock()
    supabase = Mock()
    supabase.table.return_value = table
    monkeypatch.setattr(audit_logging, "supabase", supabase)
    middleware = AuditLoggingMiddleware(app=Mock())

    await middleware._create_audit_log(
        account_id="account-1",
        action="create",
        resource_type="account",
        resource_id=None,
        details=["Authorization: Bearer auth-secret"],
        ip_address="127.0.0.1",
        user_email="user@example.test",
        path="/api/accounts",
        status_code=200,
    )

    inserted = table.insert.call_args.args[0]
    assert inserted["details"]["body"] == [f"Authorization: Bearer {REDACTED}"]
    assert inserted["details"]["path"] == "/api/accounts"
    assert inserted["details"]["status_code"] == 200
