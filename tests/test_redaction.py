"""Tests for centralized credential redaction helpers."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom import redaction
from mindroom.redaction import (
    REDACTED,
    REDACTION_FAILED,
    redact_log_event,
    redact_sensitive_data,
    redact_sensitive_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_redact_sensitive_data_redacts_nested_dicts_lists_and_header_variants() -> None:
    """Nested values and case-insensitive header spellings should be redacted."""
    payload = {
        "headers": {
            "Authorization": "Bearer auth-secret",
            "COOKIE": "session=secret",
            "set-cookie": "session=secret",
            "X-Api-Key": "api-secret",
            "x-token": "token-secret",
            "x-amz-security-token": "security-token-secret",
            "authentication-info": "auth-info-secret",
            "www-authenticate": "Bearer challenge",
            "x-ratelimit-remaining-tokens": "99",
            "x-total-tokens": "100",
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
            "x-token": REDACTED,
            "x-amz-security-token": REDACTED,
            "authentication-info": REDACTED,
            "www-authenticate": REDACTED,
            "x-ratelimit-remaining-tokens": "99",
            "x-total-tokens": "100",
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


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://journal_user:hunter2@db.example:5432/journal",
        "postgres://journal_user:hunter2@db.example/journal",
        "postgresql://journal_user@db.example:5432/journal?password=hunter2",
        "postgresql://db.example/journal?sslpassword=hunter2",
        "host=db.example dbname=journal user=journal_user password=hunter2",
    ],
)
def test_a_database_password_does_not_survive_any_of_its_spellings(dsn: str) -> None:
    """Userinfo credentials belong to the URI grammar, not to HTTP.

    A ``postgresql://`` DSN used to walk straight through, because the URL
    scan matched ``http`` and ``https`` only, so nothing ever looked at its
    userinfo. A password reaches a connection string through at least four
    different spellings and every one of them lands in the same logs.
    """
    redacted = redact_sensitive_text(dsn)

    assert "hunter2" not in redacted
    assert "db.example" in redacted, "an operator still has to be able to tell which server this was"


def test_a_scheme_carrying_no_credentials_is_left_alone() -> None:
    """Widening the scan must not start rewriting URLs that hold nothing secret."""
    assert redact_sensitive_text("postgresql://db.example:5432/journal") == "postgresql://db.example:5432/journal"


def test_redact_url_in_escaped_shell_command_keeps_json_arguments_valid() -> None:
    """URL redaction must not eat the backslash escaping the quote after the URL.

    Logged tool-call arguments are JSON-encoded strings; absorbing the trailing
    backslash of an escaped quote into the URL query re-encodes it to %5C and
    leaves a bare quote behind, corrupting the inner JSON.
    """
    command = 'curl -s \\"https://example.test/repos/demo/pulls?state=open&sort=updated&per_page=10\\" | head'
    arguments = json.dumps({"args": command})
    payload = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"function": {"name": "run_shell_command", "arguments": arguments}}]},
        ],
    }

    redacted = redact_sensitive_data(payload)

    redacted_arguments = redacted["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert REDACTED in redacted_arguments
    parsed = json.loads(redacted_arguments)
    assert parsed["args"].startswith('curl -s \\"https://example.test/repos/demo/pulls?state=***redacted***')
    assert '\\" | head' in parsed["args"]


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


def test_redact_sensitive_text_fails_closed_when_internal_redaction_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redaction bug must suppress output instead of escaping into application code."""

    def raise_redaction_error(_value: str) -> str:
        raise RuntimeError

    monkeypatch.setattr(redaction, "_redact_secret_assignments", raise_redaction_error)

    assert redact_sensitive_text("password=hunter2") == REDACTION_FAILED


def test_redact_log_event_fails_closed_when_structured_redaction_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structlog must receive a valid event even when the redactor itself fails."""

    def raise_redaction_error(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError

    monkeypatch.setattr(redaction, "_redact_sensitive_data", raise_redaction_error)

    assert redact_log_event(None, "error", {"event": "failed", "password": "hunter2"}) == {
        "event": REDACTION_FAILED,
    }


def test_redact_sensitive_data_uses_generic_mapping_fallback_when_internal_redaction_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured redaction bug must preserve mapping shape without claiming a log event."""

    def raise_redaction_error(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError

    monkeypatch.setattr(redaction, "_redact_sensitive_data", raise_redaction_error)

    assert redact_sensitive_data({"command": "safe"}) == {
        "__redaction_failed__": REDACTION_FAILED,
    }


def test_redact_sensitive_data_bounds_cyclic_containers() -> None:
    """A cyclic log payload must produce finite output without raising RecursionError."""
    value: dict[str, object] = {}
    value["self"] = value

    redacted = redact_sensitive_data(value)

    assert len(json.dumps(redacted)) < 10_000


def test_redact_log_event_bounds_large_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    """One oversized event must not make the logging processor walk every item."""
    context_label_checks = 0
    original_is_context_secret_label_key = redaction._is_context_secret_label_key

    def count_context_label_checks(value: object) -> bool:
        nonlocal context_label_checks
        context_label_checks += 1
        return original_is_context_secret_label_key(value)

    monkeypatch.setattr(redaction, "_is_context_secret_label_key", count_context_label_checks)
    event: dict[str, object] = {"value": "hunter2"}
    event.update({f"field_{index}": index for index in range(1_998)})
    event["name"] = "password"

    redacted = redact_log_event(None, "info", event)

    assert len(redacted) == 101
    assert redacted["__truncated__"] == "1900 more items"
    assert redacted["value"] == REDACTED
    assert context_label_checks == 0


def test_redact_sensitive_text_fails_closed_on_oversized_unbounded_input() -> None:
    """Unbounded callers must not make redaction scan arbitrarily large text."""
    value = "ordinary diagnostic text " * 100_000

    assert redact_sensitive_text(value) == REDACTION_FAILED


def test_redact_sensitive_text_fails_closed_on_ambiguous_multiline_secret() -> None:
    """Multiline assignment syntax is ambiguous, so suppress it instead of guessing a span."""
    value = "password=\n  hunter2\nmode=safe"

    assert redact_sensitive_text(value) == REDACTION_FAILED


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
            "has_credentials": True,
            "show_passwords": False,
            "num_secrets": 2,
            "backup_credentials": ["plain-backup-secret"],
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
        "has_credentials": True,
        "show_passwords": False,
        "num_secrets": 2,
        "backup_credentials": [REDACTED],
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


def test_redact_sensitive_text_rejects_oversized_unbounded_runs_quickly() -> None:
    """Hard input budget must reject oversized text without scanning its contents."""
    blob = "Ab3" * 40_000
    start = time.perf_counter()
    assert redact_sensitive_text(blob) == REDACTION_FAILED
    assert time.perf_counter() - start < 5.0


def test_redact_sensitive_text_stays_linear_while_finding_value_terminator() -> None:
    """Assignment lookahead must not repeatedly rescan long whitespace and key-like runs."""
    value = "password=visible" + " " * 12_000 + "Ab3" * 4_000
    start = time.perf_counter()
    assert redact_sensitive_text(value) == f"password={REDACTED}"
    assert time.perf_counter() - start < 5.0


def test_redact_sensitive_text_handles_deep_assignments_without_recursion() -> None:
    """Non-secret wrappers must not add Python stack frames while finding a secret leaf."""
    value = "api_key=hunter2"
    for _ in range(2_000):
        value = f"outer='{value}'"

    assert redact_sensitive_text(value) == value.replace("hunter2", REDACTED)


def test_redact_sensitive_text_redacts_quoted_secret_with_escaped_quote() -> None:
    """An escaped quote inside a secret must not end the redacted value early."""
    value = r'{"password": "hun\"ter2", "mode": "safe"}'

    assert redact_sensitive_text(value) == r'{"password": "***redacted***", "mode": "safe"}'


def test_redact_sensitive_text_redacts_secret_assignments_with_long_keys() -> None:
    """Performance guards must not exempt long secret-bearing keys from redaction."""
    key = "x" * 256 + "password"

    assert redact_sensitive_text(f"{key}=hunter2") == f"{key}={REDACTED}"


def test_redact_sensitive_text_preserves_long_inter_assignment_whitespace() -> None:
    """Linear lookahead must not consume whitespace that separates assignments."""
    separator = " " * 256

    assert redact_sensitive_text(f"api_key=hunter2{separator}mode=safe") == f"api_key={REDACTED}{separator}mode=safe"


def test_redact_sensitive_text_still_redacts_assignments_at_run_boundaries() -> None:
    """The assignment key guard must not lose ordinary key=value redaction."""
    redacted = redact_sensitive_text('api_key=hunter2 "password": "abc"')

    assert "hunter2" not in redacted
    assert "abc" not in redacted
    assert "api_key" in redacted


def _count_key_normalizations(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Count how many keys get normalized from scratch rather than served from cache.

    ``_classify_key_text`` resolves ``_normalize_key_text`` as a module global on
    every call, so this probe sees the work even when it runs behind the cache.
    """
    calls = 0
    original_normalize = redaction._normalize_key_text

    def counting_normalize(key: str) -> str:
        nonlocal calls
        calls += 1
        return original_normalize(key)

    monkeypatch.setattr(redaction, "_normalize_key_text", counting_normalize)

    def take() -> int:
        nonlocal calls
        count, calls = calls, 0
        return count

    return take


def _key_normalization_cache_size() -> int:
    """Return the cache size for test assertions."""
    return redaction._classify_key_text_cached.cache_info().currsize


def test_repeated_log_events_normalize_each_key_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """High-frequency structured logs repeat the same keys and must not re-normalize them."""
    # Unique keys keep the first event cold regardless of what earlier tests cached.
    unique = uuid4().hex
    event = {
        f"event_{unique}": "dispatch delivery timing",
        f"phase_{unique}": "queued",
        f"queue_size_{unique}": 7,
        f"progress_hint_{unique}": True,
        f"boundary_refresh_{unique}": False,
        f"timing_scope_{unique}": "scope-value",
    }
    take_count = _count_key_normalizations(monkeypatch)

    redact_log_event(None, "debug", dict(event))
    first_event_calls = take_count()
    redact_log_event(None, "debug", dict(event))
    second_event_calls = take_count()

    assert first_event_calls == len(event)
    assert second_event_calls == 0


def test_key_normalization_cache_is_bounded() -> None:
    """An unbounded cache keyed on arbitrary log keys would leak, so it must evict."""
    distinct_keys = 50_000

    for start in range(0, distinct_keys, 100):
        redact_log_event(
            None,
            "debug",
            {f"field_{index}": index for index in range(start, start + 100)},
        )

    assert 0 < _key_normalization_cache_size() < distinct_keys


def test_oversized_log_keys_are_not_cached_but_still_redact() -> None:
    """Pathologically long keys must bypass the cache so its memory stays bounded."""
    long_secret_key = "x" * 4096 + "_api_key"
    before = _key_normalization_cache_size()

    redacted = redact_sensitive_data({long_secret_key: "plain-secret"})

    assert redacted == {long_secret_key: REDACTED}
    assert _key_normalization_cache_size() == before


# Key classification is security-relevant, so it is pinned explicitly rather than
# derived: a cache that reclassified any of these would silently change redaction.
_REDACTED_KEYS = (
    "ACCESS-TOKEN",
    "API_KEY",
    "AUTHORIZATION",
    "ApiKey",
    "Bearer_Token",
    "Cookie",
    "HTTPAPIKey",
    "Password",
    "TOKEN",
    "Token",
    "X-Api-Key",
    "accessToken",
    "access_token",
    "api-key",
    "apiKey",
    "api_key",
    "api_keys",
    "auth_token",
    "authentication-info",
    "authorization",
    "backup_credentials",
    "clientSecret",
    "client_secret",
    "cookie",
    "credentials",
    "has_credentials",
    "id_token",
    "num_secrets",
    "oauth_tokens",
    "openai_api_key",
    "password",
    "password_policy",
    "refreshToken",
    "secret_sauce_recipe",
    "secrets",
    "security_token",
    "session_token",
    "set-cookie",
    "show_passwords",
    "token",
    "tokens",
    "user-password",
    "user.password",
    "www-authenticate",
    "x-api-key",
    "x_token",
)
_KEPT_KEYS = (
    "XMLHttpToken",
    "input_tokens",
    "max_tokens",
    "message",
    "my_token",
    "name",
    "next_token",
    "output_tokens",
    "queue_size",
    "tokenCount",
    "tokenizer",
    "x-ratelimit-remaining-tokens",
)


@pytest.mark.parametrize("key", _REDACTED_KEYS)
def test_secret_key_variants_redact_identically_when_cached_and_uncached(key: str) -> None:
    """Memoized lookups must classify every case/format variant exactly as a cold lookup does."""
    cold = redact_sensitive_data({key: "plain-secret"})
    warm = redact_sensitive_data({key: "plain-secret"})

    assert cold == {key: REDACTED}
    assert warm == cold


@pytest.mark.parametrize("key", _KEPT_KEYS)
def test_non_secret_key_variants_survive_identically_when_cached_and_uncached(key: str) -> None:
    """Memoization must not start redacting keys that a cold lookup keeps."""
    cold = redact_sensitive_data({key: "kept-value"})
    warm = redact_sensitive_data({key: "kept-value"})

    assert cold == {key: "kept-value"}
    assert warm == cold


def test_cache_eviction_does_not_change_key_classification() -> None:
    """Classification must survive eviction: a re-resolved key must match its first result."""
    probe_keys = (*_REDACTED_KEYS, *_KEPT_KEYS)
    before = {key: redact_sensitive_data({key: "probe-value"}) for key in probe_keys}

    # Flood the cache with far more distinct keys than it can hold, across bounded events.
    for start in range(0, 50_000, 100):
        redact_log_event(
            None,
            "debug",
            {f"flood_{index}": index for index in range(start, start + 100)},
        )

    assert {key: redact_sensitive_data({key: "probe-value"}) for key in probe_keys} == before
