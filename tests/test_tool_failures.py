"""Tests for durable tool failure logging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mindroom.constants import tracking_dir
from mindroom.tool_system import tool_failures
from mindroom.tool_system.tool_failures import build_tool_failure_record, record_tool_failure
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import test_runtime_paths


@pytest.fixture(autouse=True)
def reset_failure_loggers() -> None:
    """Reset cached rotating loggers so tests do not leak global handler state."""
    tool_failures._reset_failure_loggers_for_tests()


def _execution_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$resolved-thread",
        session_id="session-1",
    )


class _BadStr:
    def __str__(self) -> str:
        msg = "str disabled"
        raise RuntimeError(msg)


class _BadRepr:
    def __repr__(self) -> str:
        msg = "repr disabled"
        raise RuntimeError(msg)


def test_build_tool_failure_record_redacts_nested_arguments_and_urls() -> None:
    """Nested mappings, tokens, and URL credentials should be sanitized in persisted records."""
    error = RuntimeError("payload={'api_key': 'secret-value'}")
    record = build_tool_failure_record(
        tool_name="demo",
        arguments={
            "path": "notes.txt",
            "nested": {
                "api_key": "secret-value",
                "items": [
                    {"url": "https://alice:secret@example.com/private"},
                    {"authorization": "Bearer hidden-token"},
                ],
            },
            "tokens": [{"refresh_token": "refresh-secret"}],
        },
        error=error,
        duration_ms=12.345,
        agent_name="code",
        channel="matrix",
        room_id="!room:localhost",
        thread_id="$resolved-thread",
        requester_id="@user:localhost",
        session_id="session-1",
        correlation_id="corr-1",
    )

    assert record.arguments == {
        "path": "notes.txt",
        "nested": {
            "api_key": "***redacted***",
            "items": [
                {"url": "https://alice:***@example.com/private"},
                {"authorization": "***redacted***"},
            ],
        },
        "tokens": [{"refresh_token": "***redacted***"}],
    }
    assert "secret-value" not in record.error_message
    assert "***redacted***" in record.error_message


def test_sanitize_failure_text_redacts_url_credentials() -> None:
    """Credential-bearing HTTP(S) URLs should be masked in free-form error text."""
    sanitized = tool_failures.sanitize_failure_text("clone failed for https://alice:secret@example.com/private.git")
    assert sanitized == "clone failed for https://alice:***@example.com/private.git"


def test_sanitize_failure_text_redacts_signed_url_query_credentials() -> None:
    """Signed query-string credentials should be redacted alongside basic-auth URL credentials."""
    sanitized = tool_failures.sanitize_failure_text(
        "fetch failed for "
        "https://alice:secret@example.com/private?"
        "sig=azure-secret&X-Amz-Signature=s3-signature&X-Amz-Credential=s3-credential"
        "&X-Amz-Security-Token=session-token&api_key=query-secret&keep=1",
    )

    assert sanitized == (
        "fetch failed for "
        "https://alice:***@example.com/private?"
        "sig=***redacted***&X-Amz-Signature=***redacted***&X-Amz-Credential=***redacted***"
        "&X-Amz-Security-Token=***redacted***&api_key=***redacted***&keep=1"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Incorrect API key provided: sk-secret123", "Incorrect API key provided: ***redacted***"),
        ("Invalid API key sk-secret123", "Invalid API key ***redacted***"),
        ("Bearer abc123", "Bearer ***redacted***"),
        ("Authorization header Bearer abc123", "Authorization header Bearer ***redacted***"),
        ("Bearer token abc123 expired", "Bearer token ***redacted*** expired"),
        (
            "sk-secret123 pk-secret456 xoxb-secret789 xoxp-secret000",
            "***redacted*** ***redacted*** ***redacted*** ***redacted***",
        ),
    ],
)
def test_sanitize_failure_text_redacts_common_sdk_secret_phrasings(raw: str, expected: str) -> None:
    """Common SDK authentication errors should not leak tokens into durable logs."""
    assert tool_failures.sanitize_failure_text(raw) == expected


def test_sanitize_failure_text_redacts_additional_provider_secret_formats() -> None:
    """Provider-specific token formats should be recognized outside generic API-key phrasings."""
    sanitized = tool_failures.sanitize_failure_text(
        "sk_live_secret rk_live_secret ghp_secret github_pat_secret AIzaSySecret",
    )

    assert sanitized == (
        "***redacted*** ***redacted*** ***redacted*** ***redacted*** ***redacted***"
    )


@pytest.mark.parametrize(
    ("secret_key", "prefixed_key"),
    [
        ("apiKey", "openaiApiKey"),
        ("clientSecret", "oauthClientSecret"),
        ("accessToken", "githubAccessToken"),
        ("refreshToken", "matrixRefreshToken"),
    ],
)
def test_build_tool_failure_record_redacts_camel_case_and_prefixed_secret_keys(
    secret_key: str,
    prefixed_key: str,
) -> None:
    """CamelCase and prefixed secret keys should redact in structured values and text."""
    record = build_tool_failure_record(
        tool_name="demo",
        arguments={
            secret_key: "secret-value",
            prefixed_key: "prefixed-secret",
        },
        error=RuntimeError(
            f"{secret_key}=secret-value {prefixed_key}='prefixed-secret' payload={{'{secret_key}': 'secret-value'}}",
        ),
        duration_ms=1.0,
        agent_name="code",
        channel="matrix",
        room_id="!room:localhost",
        thread_id="$resolved-thread",
        requester_id="@user:localhost",
        session_id="session-1",
        correlation_id="corr-camel",
    )

    assert record.arguments == {
        secret_key: "***redacted***",
        prefixed_key: "***redacted***",
    }
    assert "secret-value" not in record.error_message
    assert "prefixed-secret" not in record.error_message
    assert record.error_message.count("***redacted***") == 3


def test_sanitize_failure_text_redacts_camel_case_secret_assignments() -> None:
    """CamelCase secret assignments should redact across quoted and unquoted forms."""
    sanitized = tool_failures.sanitize_failure_text(
        "apiKey=secret clientSecret='top-secret' accessToken: token-value "
        'refreshToken="refresh-value" openaiApiKey=provider-secret',
    )

    assert sanitized == (
        "apiKey=***redacted*** clientSecret='***redacted***' accessToken: ***redacted*** "
        'refreshToken="***redacted***" openaiApiKey=***redacted***'
    )


@pytest.mark.parametrize(
    "secret_key",
    [
        "secret_key",
        "apiSecretKey",
        "authorization_header",
        "refreshTokenValue",
        "myCustomSecret",
    ],
)
def test_sanitize_failure_redacts_secret_key_suffix_variants(secret_key: str) -> None:
    """Secret-bearing stems should redact even when the key has additional suffix components."""
    assert tool_failures.sanitize_failure_value({secret_key: "topsecret"}) == {
        secret_key: "***redacted***",
    }
    assert tool_failures.sanitize_failure_text(f"{secret_key}=topsecret") == f"{secret_key}=***redacted***"


def test_sanitize_failure_value_replaces_non_finite_floats() -> None:
    """NaN and infinity should be normalized before persistence to JSONL."""
    assert tool_failures.sanitize_failure_value(
        {
            "nan": float("nan"),
            "pos_inf": float("inf"),
            "neg_inf": float("-inf"),
            "finite": 1.5,
        },
    ) == {
        "nan": None,
        "pos_inf": None,
        "neg_inf": None,
        "finite": 1.5,
    }


def test_sanitize_failure_value_handles_unrepresentable_keys_and_values() -> None:
    """Custom objects with broken __str__ or __repr__ should not abort sanitization."""
    sanitized = tool_failures.sanitize_failure_value(
        {
            _BadStr(): "kept",
            "value": _BadRepr(),
        },
    )

    assert sanitized == {
        "<unrepresentable: _BadStr>": "kept",
        "value": "<unrepresentable: _BadRepr>",
    }


def test_build_tool_failure_record_truncates_tracebacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracebacks should be sanitized before length limits are applied."""
    monkeypatch.setattr(tool_failures, "_MAX_TRACEBACK_LENGTH", 120)

    def explode() -> None:
        msg = "token=secret-value " + ("x" * 200)
        raise RuntimeError(msg)

    try:
        explode()
    except RuntimeError as error:
        record = build_tool_failure_record(
            tool_name="explode",
            arguments={},
            error=error,
            duration_ms=1.0,
            agent_name="code",
            channel="matrix",
            room_id="!room:localhost",
            thread_id="$resolved-thread",
            requester_id="@user:localhost",
            session_id="session-1",
            correlation_id="corr-2",
        )

    assert len(record.traceback) == 120
    assert record.traceback.endswith("... [truncated]")
    assert "secret-value" not in record.traceback


def test_record_tool_failure_writes_jsonl_and_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure records should append to JSONL and honor rotation limits."""
    runtime_paths = test_runtime_paths(tmp_path)
    log_path = tracking_dir(runtime_paths) / "tool_failures.jsonl"
    backup_path = Path(f"{log_path}.1")

    monkeypatch.setattr(tool_failures, "_FAILURE_LOG_MAX_BYTES", 300)
    monkeypatch.setattr(tool_failures, "_FAILURE_LOG_BACKUPS", 1)

    for index in range(6):
        record_tool_failure(
            tool_name="explode",
            arguments={"payload": "x" * 200, "api_key": "secret"},
            error=RuntimeError(f"boom-{index}"),
            duration_ms=10.0 + index,
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$resolved-thread",
            requester_id="@user:localhost",
            session_id="session-1",
            correlation_id=f"corr-{index}",
            execution_identity=_execution_identity(),
            runtime_paths=runtime_paths,
        )

    assert log_path.exists()
    assert backup_path.exists()

    parsed_lines = [
        json.loads(line)
        for path in (backup_path, log_path)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert parsed_lines
    assert all(line["tool_name"] == "explode" for line in parsed_lines)


def test_record_tool_failure_logs_secondary_write_errors(tmp_path: Path) -> None:
    """JSONL write failures should be logged without replacing the original record."""
    runtime_paths = test_runtime_paths(tmp_path)

    with (
        patch("mindroom.tool_system.tool_failures._append_failure_record", side_effect=OSError("disk full")),
        patch("mindroom.tool_system.tool_failures.logger.exception") as mock_logger_exception,
    ):
        record = record_tool_failure(
            tool_name="explode",
            arguments={"api_key": "secret"},
            error=RuntimeError("boom"),
            duration_ms=10.0,
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$resolved-thread",
            requester_id="@user:localhost",
            session_id="session-1",
            correlation_id="corr-write-fail",
            execution_identity=_execution_identity(),
            runtime_paths=runtime_paths,
        )

    assert record.tool_name == "explode"
    assert record.error_type == "RuntimeError"
    mock_logger_exception.assert_called_once_with(
        "Failed to persist tool failure record",
        tool_name="explode",
        correlation_id="corr-write-fail",
    )


def test_record_tool_failure_skips_persistence_without_runtime_paths() -> None:
    """The durable record should still be built when runtime paths are unavailable."""
    with patch("mindroom.tool_system.tool_failures._append_failure_record") as mock_append:
        record = record_tool_failure(
            tool_name="explode",
            arguments={"api_key": "secret"},
            error=RuntimeError("boom"),
            duration_ms=10.0,
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$resolved-thread",
            requester_id="@user:localhost",
            session_id="session-1",
            correlation_id="corr-no-runtime",
            execution_identity=_execution_identity(),
            runtime_paths=None,
        )

    assert record.tool_name == "explode"
    assert record.arguments == {"api_key": "***redacted***"}
    mock_append.assert_not_called()


def test_build_tool_failure_record_uses_redaction_markers_and_truncates_large_payloads() -> None:
    """Large values should stay bounded while preserving explicit redaction markers."""
    record = build_tool_failure_record(
        tool_name="demo",
        arguments={
            "api_key": "secret",
            "payload": "x" * 5000,
            "items": [str(index) for index in range(30)],
        },
        error=RuntimeError("cookie=session-secret"),
        duration_ms=5.0,
        agent_name="code",
        channel="matrix",
        room_id="!room:localhost",
        thread_id="$resolved-thread",
        requester_id="@user:localhost",
        session_id="session-1",
        correlation_id="corr-3",
    )

    assert record.arguments["api_key"] == "***redacted***"
    assert record.arguments["payload"].endswith("... [truncated]")
    assert len(record.arguments["payload"]) == tool_failures._MAX_STRING_LENGTH
    assert record.arguments["items"][-1] == "... [truncated]"
    assert record.error_message == "cookie=***redacted***"


def test_sanitize_failure_value_truncates_at_max_redaction_depth() -> None:
    """Nested values beyond the configured redaction depth should be truncated."""
    assert tool_failures.sanitize_failure_value(
        {"a": {"b": {"c": {"d": {"e": {"f": {"g": "secret"}}}}}}},
    ) == {
        "a": {
            "b": {
                "c": {
                    "d": {
                        "e": {
                            "f": tool_failures._TRUNCATED,
                        },
                    },
                },
            },
        },
    }
