"""Regression tests for audit-log credential redaction."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from backend.middleware import audit_logging
from backend.middleware.audit_logging import AuditLoggingMiddleware
from backend.utils.audit import (
    MAX_AUDIT_DEPTH,
    MAX_AUDIT_TEXT_LENGTH,
    REDACTED,
    TRUNCATED,
    redact_audit_details,
    redact_audit_text,
)


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


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 1_000_000, id="delimiter-free-run"),
        pytest.param("A" * 1_000_000, id="uppercase-run"),
        pytest.param("a=" * 500_000, id="chained-assignments"),
        pytest.param("a='" * 300_000, id="chained-quoted-assignments"),
        pytest.param("token=x," * 125_000, id="many-secret-assignments"),
    ],
)
def test_redact_audit_details_is_bounded_on_adversarial_strings(text: str) -> None:
    """Redaction must stay fast on long attacker-controlled strings and keys."""
    started = time.perf_counter()
    redacted = redact_audit_details({"value": text, text: "key"})
    elapsed = time.perf_counter() - started

    assert elapsed < 1
    assert all(len(key) <= MAX_AUDIT_TEXT_LENGTH for key in redacted)
    assert all(len(value) <= MAX_AUDIT_TEXT_LENGTH for value in redacted.values())


def test_redact_audit_details_bounds_quoted_secrets_that_never_close() -> None:
    """Quoted secret values whose inner quotes never end them are delimited without rescanning the text."""
    started = time.perf_counter()
    redact_audit_details({str(index): "token='a'b " * 420 for index in range(20)})

    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    "children",
    [
        pytest.param([""] * 5_000, id="list-items"),
        pytest.param({f"k{index}": "" for index in range(5_000)}, id="child-keys"),
    ],
)
def test_redact_audit_details_classifies_each_key_once(children: object) -> None:
    """A long key is not renormalized for every child value or key beneath it."""
    started = time.perf_counter()
    redact_audit_details({"AAa" * 1_365: children})

    assert time.perf_counter() - started < 1


def test_redact_audit_text_truncates_after_redacting_the_cut_region() -> None:
    """Long audit text is truncated, and a secret that straddles the cut is still masked."""
    text = "x" * (MAX_AUDIT_TEXT_LENGTH - 20) + " password=" + "s" * 100 + " tail"

    redacted = redact_audit_text(text)

    assert len(redacted) == MAX_AUDIT_TEXT_LENGTH
    assert redacted.endswith(TRUNCATED)
    assert "s" * 5 not in redacted


def test_redact_audit_text_redacts_secret_assignments_after_non_secret_keys() -> None:
    """Secret assignments nested behind ordinary keys are still masked without recursion."""
    assert redact_audit_text("note: password=pw-secret") == f"note: password={REDACTED}"
    assert "pw-secret" not in redact_audit_text('config="password=pw-secret", ok=1')
    assert redact_audit_text('{"password": "pw-secret", "name": "kept"}') == (
        f'{{"password": "{REDACTED}", "name": "kept"}}'
    )
    assert "tok-secret" not in redact_audit_text("a=" * 1_000 + "token=tok-secret")


@pytest.mark.parametrize(
    "text",
    [
        "password='it's-hunter2' user=bob",
        "login failed password='o'reilly-hunter2', user=bob",
        'token="say "hunter2" twice" and next=1',
    ],
)
def test_redact_audit_text_does_not_close_quoted_values_at_inner_quotes(text: str) -> None:
    """A quote inside a quoted secret does not end it unless a delimiter or the next assignment follows."""
    assert "hunter2" not in redact_audit_text(text)


def test_redact_audit_details_redacts_unparseable_urls_instead_of_failing() -> None:
    """A malformed URL must not make redaction raise, which would drop the whole audit row."""
    assert redact_audit_details({"note": "see http://[broken", "ok": "kept"}) == {
        "note": f"see {REDACTED}",
        "ok": "kept",
    }


def test_redact_audit_details_bounds_nesting_depth() -> None:
    """Deeply nested details are cut off instead of recursing without limit."""
    nested: object = "leaf"
    for _ in range(MAX_AUDIT_DEPTH * 4):
        nested = [nested]

    redacted = redact_audit_details({"nested": nested})

    depth = 0
    current = redacted["nested"]
    while isinstance(current, list):
        current = current[0]
        depth += 1
    assert current == TRUNCATED
    assert depth < MAX_AUDIT_DEPTH
