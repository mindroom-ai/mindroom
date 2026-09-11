"""Tests for the active-response CLI contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _snapshot(**overrides: object) -> dict[str, object]:
    return {
        "status": "idle",
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": 0,
        "active_openai_requests": 0,
        **overrides,
    }


@pytest.mark.parametrize(
    ("payload", "exit_code", "status"),
    [
        (_snapshot(), 0, "idle"),
        (_snapshot(active_matrix_operations=2, status="busy"), 1, "busy"),
        (_snapshot(active_openai_requests=1, status="busy"), 1, "busy"),
        (_snapshot(runtime_phase="starting", status="unavailable"), 2, "unavailable"),
        (_snapshot(admission_paused=True, status="unavailable"), 2, "unavailable"),
        (_snapshot(active_matrix_operations=None, admission_paused=None, status="unavailable"), 2, "unavailable"),
    ],
)
def test_cli_exit_codes_and_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, object],
    exit_code: int,
    status: str,
) -> None:
    """Only a ready, bound, idle snapshot permits exit zero."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["status"] == status


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"status": "idle"},
        _snapshot(active_matrix_operations=-1),
        _snapshot(active_openai_requests=True),
        _snapshot(active_matrix_operations="0"),
        _snapshot(admission_paused="false"),
    ],
)
def test_cli_rejects_incomplete_or_malformed_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> None:
    """Missing or invalid counters must never be treated as zero."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 503, 302])
def test_cli_http_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
) -> None:
    """Auth, old servers, redirects, and server failures cannot return idle."""
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(status_code, text="not a snapshot"))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_cli_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stalled API must yield an unavailable result, not a traceback."""

    def fail(*_args: object, **_kwargs: object) -> httpx.Response:
        message = "stalled"
        raise httpx.ReadTimeout(message)

    monkeypatch.setattr(httpx, "get", fail)
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_cli_uses_selected_environment_and_explicit_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Selected env credentials and explicit URL must reach the API without leaking the token."""
    (tmp_path / ".env").write_text("MINDROOM_URL=http://unused.test\nMINDROOM_API_KEY=test-secret\n")
    monkeypatch.delenv("MINDROOM_API_KEY", raising=False)
    monkeypatch.delenv("MINDROOM_URL", raising=False)

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "http://localhost:9876/base/api/responses/activity"
        assert kwargs["headers"] == {"Authorization": "Bearer test-secret"}
        assert kwargs["timeout"] == 3.0
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json=_snapshot())

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(
        app,
        [
            "check-active-responses",
            "--config",
            str(tmp_path / "config.yaml"),
            "--url",
            "http://localhost:9876/base/",
            "--timeout",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No active responses" in result.stdout
    assert "test-secret" not in result.output


@pytest.mark.parametrize(
    "url",
    ["http://localhost:bad", "file:///activity", "http://secret@localhost"],
)
def test_cli_invalid_url_fails_closed(tmp_path: Path, url: str) -> None:
    """Bad URLs must produce a controlled unavailable result without exposing input."""
    result = runner.invoke(
        app,
        ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--url", url, "--json"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert "secret@" not in result.output


@pytest.mark.parametrize("wire_status", ["busy", "unavailable", "future-status", None])
def test_cli_rejects_conflicting_or_missing_wire_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wire_status: str | None,
) -> None:
    """Server-reported uncertainty must never be overwritten by idle counters."""
    payload = _snapshot()
    if wire_status is None:
        payload.pop("status", None)
    else:
        payload["status"] = wire_status
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["check-active-responses", "--config", str(tmp_path / "config.yaml"), "--json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "unavailable"
