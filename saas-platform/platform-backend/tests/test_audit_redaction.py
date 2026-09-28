"""Regression tests for audit-log credential redaction."""

from __future__ import annotations

import time

import pytest
from backend.utils.audit import MAX_AUDIT_TEXT_LENGTH, REDACTED, TRUNCATED, redact_audit_details, redact_audit_text


def test_redact_audit_details_recurses_and_matches_case_insensitive_headers() -> None:
    """Audit details should keep shape while masking nested bearer material."""
    details = {
        "headers": {"Authorization": "Bearer auth-secret", "COOKIE": "session=secret", "set-cookie": "session=secret"},
        "body": {"access_token": "access-secret", "nested": [{"clientSecret": "client-secret"}, {"safe": "kept"}]},
    }

    assert redact_audit_details(details) == {
        "headers": {"Authorization": REDACTED, "COOKIE": REDACTED, "set-cookie": REDACTED},
        "body": {"access_token": REDACTED, "nested": [{"clientSecret": REDACTED}, {"safe": "kept"}]},
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


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("a", id="delimiter-free-run"),
        pytest.param("A", id="uppercase-run"),
        pytest.param("a=", id="chained-assignments"),
        pytest.param("a='", id="chained-quoted-assignments"),
        pytest.param("token=x,", id="many-secret-assignments"),
        pytest.param("http://?code='", id="urls-that-grow-when-redacted"),
        pytest.param(".:", id="short-assignment-runs"),
    ],
)
def test_redact_audit_details_cost_is_bounded_by_the_text_cap(unit: str) -> None:
    """Only the first `MAX_AUDIT_TEXT_LENGTH` characters are redacted, so megabyte strings cost what the cap costs."""
    text = unit * (1_000_000 // len(unit))

    started = time.perf_counter()
    redacted = redact_audit_details({"value": text})

    assert time.perf_counter() - started < 0.25
    assert redacted["value"].endswith(TRUNCATED)


def test_redact_audit_text_keeps_short_text_whole() -> None:
    """Text within the cap is redacted and kept whole."""
    text = "x" * (MAX_AUDIT_TEXT_LENGTH - len(" password=pw-secret")) + " password=pw-secret"

    assert redact_audit_text(text) == text.replace("pw-secret", REDACTED)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password='it's-hunter2' user=bob", "hunter2"),
        ("login failed password='o'reilly-hunter2', user=bob", "hunter2"),
        ('token="say "hunter2" twice" and next=1', "hunter2"),
        ("retry with password=)Qw9!zz user=bob", "Qw9!zz"),
        ("token=}abc123def", "abc123def"),
        ("api_key: ,sk_abc", "sk_abc"),
        ("password=]P4ss", "P4ss"),
        ("password='}pw-secret\\'", "pw-secret"),
        ('password="abc\rcr-secret" user=bob', "cr-secret"),
        ("note: password=pw-secret", "pw-secret"),
        ('config="password=pw-secret", ok=1', "pw-secret"),
        ("https://admin:HunterTwoPw@db.example.com/x", "HunterTwoPw"),
        ("https://app.example/login?next=https://admin:hunter2@db.internal/x", "hunter2"),
        ("{'detail': \"password='abc,def'\"}", "def"),
        ("{'detail': \"password='abc,def'\"}", "abc"),
        ('{"password": "pw-secret", "name": "kept"}', "pw-secret"),
        ("a=" * 200 + "token=tok-secret", "tok-secret"),
    ],
)
def test_redact_audit_text_redacts_review_leak_cases(text: str, secret: str) -> None:
    """Secrets from every review round stay redacted: inner quotes, leading delimiters, and nested values."""
    assert secret not in redact_audit_text(text)


def test_redact_audit_details_redacts_unparseable_urls_instead_of_failing() -> None:
    """A malformed URL must not make redaction raise, which would drop the whole audit row."""
    assert redact_audit_details({"note": "see http://[broken", "ok": "kept"}) == {
        "note": f"see {REDACTED}",
        "ok": "kept",
    }
