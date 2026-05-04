"""Tests for structured logging configuration."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, NoReturn

import pytest

from mindroom.constants import RuntimePaths
from mindroom.logging_config import bound_log_context, get_logger, setup_logging
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return RuntimePaths(
        config_path=config_path,
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "mindroom_data",
    )


def _last_stderr_line(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err.strip().splitlines()[-1]


def _last_stderr_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(_last_stderr_line(capsys))


def _raise_value_error() -> NoReturn:
    msg = "boom"
    raise ValueError(msg)


def test_setup_logging_json_mode_emits_expected_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON mode should emit the expected structured fields for structlog loggers."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    get_logger("tests.logging").info("test_event", room_id="!room:example.org")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "test_event"
    assert payload["level"] == "info"
    assert payload["logger"] == "tests.logging"
    assert payload["room_id"] == "!room:example.org"
    assert "timestamp" in payload


def test_bound_log_context_from_message_target_binds_and_restores_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scoped log context should include target fields only within the active scope."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    target = MessageTarget.resolve(
        room_id="!room:example.org",
        thread_id="$thread",
        reply_to_event_id="$reply",
    )

    with bound_log_context(**target.log_context):
        get_logger("tests.logging").info("scoped_event")
    get_logger("tests.logging").info("outside_scope")

    lines = capsys.readouterr().err.strip().splitlines()
    scoped_payload = json.loads(lines[0])
    outside_payload = json.loads(lines[1])

    assert scoped_payload["event"] == "scoped_event"
    assert scoped_payload["room_id"] == "!room:example.org"
    assert scoped_payload["thread_id"] == "$thread"
    assert outside_payload["event"] == "outside_scope"
    assert "room_id" not in outside_payload
    assert "thread_id" not in outside_payload


def test_setup_logging_json_mode_includes_logger_for_foreign_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON mode should add the stdlib logger name for foreign log records."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    logging.getLogger("test.foreign").info("foreign message")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "foreign message"
    assert payload["level"] == "info"
    assert payload["logger"] == "test.foreign"
    assert "timestamp" in payload


def test_setup_logging_logger_level_overrides_allow_selective_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logger overrides should allow MindRoom debug logs without enabling every library."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    monkeypatch.setenv("MINDROOM_LOGGER_LEVELS", "mindroom:DEBUG,httpx:WARNING")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    get_logger("mindroom.test").debug("mindroom_debug_visible")
    logging.getLogger("httpx").info("httpx_info_hidden")

    lines = capsys.readouterr().err.strip().splitlines()

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "mindroom_debug_visible"
    assert payload["level"] == "debug"
    assert payload["logger"] == "mindroom.test"


def test_setup_logging_logger_level_overrides_accept_whitespace_and_semicolons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators can use comma or semicolon separated logger override lists."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    monkeypatch.setenv("MINDROOM_LOGGER_LEVELS", " mindroom : DEBUG ; anthropic : ERROR ")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    logging.getLogger("anthropic").warning("anthropic_warning_hidden")
    logging.getLogger("mindroom.example").debug("mindroom_debug_visible")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "mindroom_debug_visible"
    assert payload["logger"] == "mindroom.example"


def test_setup_logging_defaults_quiet_noisy_nio_crypto_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default logger levels should quiet noisy Matrix crypto warning bursts only."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    monkeypatch.delenv("MINDROOM_LOGGER_LEVELS", raising=False)
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    logging.getLogger("nio.crypto.log").warning("Error decrypting megolm event, no session found")
    get_logger("mindroom.delivery").warning("delivery_error_visible")

    lines = capsys.readouterr().err.strip().splitlines()

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "delivery_error_visible"
    assert payload["logger"] == "mindroom.delivery"


def test_setup_logging_allows_explicit_nio_crypto_warning_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators should be able to re-enable Matrix crypto warnings for diagnostics."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    monkeypatch.setenv("MINDROOM_LOGGER_LEVELS", "nio.crypto:WARNING")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    logging.getLogger("nio.crypto.log").warning("Error decrypting megolm event, no session found")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "Error decrypting megolm event, no session found"
    assert payload["level"] == "warning"
    assert payload["logger"] == "nio.crypto.log"


def test_setup_logging_json_mode_foreign_logger_inherits_bound_log_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Foreign loggers should include active structured room/thread context."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    with bound_log_context(room_id="!room:example.org", thread_id="$thread:example.org"):
        logging.getLogger("test.foreign").info("foreign scoped message")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "foreign scoped message"
    assert payload["logger"] == "test.foreign"
    assert payload["room_id"] == "!room:example.org"
    assert payload["thread_id"] == "$thread:example.org"


def test_setup_logging_text_mode_does_not_emit_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Text mode should keep the default console renderer instead of emitting JSON."""
    monkeypatch.delenv("MINDROOM_LOG_FORMAT", raising=False)
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    get_logger("tests.logging").info("text_mode_event", room_id="!room:example.org")

    line = _last_stderr_line(capsys)

    assert "text_mode_event" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_setup_logging_json_mode_renders_exception_field_for_exc_info_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON mode should render exc_info=True into an exception field."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    try:
        _raise_value_error()
    except ValueError:
        get_logger("tests.logging").exception("test_exception", agent="alpha")

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "test_exception"
    assert payload["agent"] == "alpha"
    assert isinstance(payload["exception"], str)
    assert "ValueError: boom" in payload["exception"]


def test_setup_logging_json_mode_renders_exception_field_for_exception_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON mode should render exception instances passed via exc_info."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    get_logger("tests.logging").error("test_exception_instance", exc_info=ValueError("boom"))

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "test_exception_instance"
    assert isinstance(payload["exception"], str)
    assert "ValueError: boom" in payload["exception"]


def test_setup_logging_json_mode_renders_exception_field_for_exc_info_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON mode should render exc_info tuples into an exception field."""
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=_runtime_paths(tmp_path))
    capsys.readouterr()

    try:
        _raise_value_error()
    except ValueError:
        get_logger("tests.logging").error("test_exception_tuple", exc_info=sys.exc_info())

    payload = _last_stderr_payload(capsys)

    assert payload["event"] == "test_exception_tuple"
    assert isinstance(payload["exception"], str)
    assert "ValueError: boom" in payload["exception"]
