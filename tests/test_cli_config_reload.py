"""The config receipt CLI must match the requested source before returning success."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.mark.parametrize(
    ("status", "same_fingerprint", "exit_code", "reported_status"),
    [
        ("applied", True, 0, "applied"),
        ("applied", False, 1, "pending"),
        ("pending", True, 1, "pending"),
        ("failed", True, 2, "failed"),
        ("failed", False, 1, "pending"),
        ("restart_required", True, 2, "restart_required"),
        ("unavailable", False, 2, "unavailable"),
    ],
)
def test_exact_source_and_result_control_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    same_fingerprint: bool,
    exit_code: int,
    reported_status: str,
) -> None:
    """Unrelated success or failure must not settle this config's deployment."""
    path = tmp_path / "config.yaml"
    source = b"{}\n"
    path.write_bytes(source)
    expected = hashlib.sha256(source).hexdigest()
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            503 if status == "unavailable" else 200,
            json={"status": status, "fingerprint": expected if same_fingerprint else "a" * 64},
        ),
    )
    result = runner.invoke(app, ["config", "check-applied", "--path", str(path), "--json"])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["status"] == reported_status


def test_wait_pins_source_before_polling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source edit during the wait cannot silently change the requested target."""
    path = tmp_path / "config.yaml"
    source = b"{}\n"
    path.write_bytes(source)
    expected = hashlib.sha256(source).hexdigest()
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    responses = iter(
        [
            {"status": "pending", "fingerprint": expected},
            {"status": "applied", "fingerprint": expected},
        ],
    )

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/api/config/reload-status")
        assert kwargs["headers"] == {"Authorization": "Bearer operator-key"}
        path.write_text("defaults: {enable_streaming: false}\n")
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(app, ["config", "check-applied", "--path", str(path), "--wait", "2", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["fingerprint"] == expected


@pytest.mark.parametrize("payload", [{}, {"status": "applied"}, {"status": "applied", "fingerprint": "invalid"}])
def test_malformed_receipt_fails_closed(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """An old or wrong endpoint must never acknowledge a config."""
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json=payload))
    result = runner.invoke(app, ["config", "check-applied", "--fingerprint", "a" * 64, "--json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_wait_timeout_remains_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expiration must return pending without starting a request after the deadline."""
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    calls = []

    def get(*_args: object, **_kwargs: object) -> httpx.Response:
        calls.append(True)
        if len(calls) > 1:
            msg = "request started after wait expired"
            raise httpx.ReadTimeout(msg)
        return httpx.Response(200, json={"status": "pending", "fingerprint": "a" * 64})

    monkeypatch.setattr(httpx, "get", get)
    result = runner.invoke(app, ["config", "check-applied", "--fingerprint", "a" * 64, "--wait", "0.02", "--json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["status"] == "pending"


def test_timeout_must_be_finite_even_when_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamping the request timeout to the wait budget must not accept infinity."""
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(200, json={"status": "applied", "fingerprint": "a" * 64}),
    )
    result = runner.invoke(
        app,
        ["config", "check-applied", "--fingerprint", "a" * 64, "--wait", "1", "--timeout", "inf", "--json"],
    )
    assert result.exit_code == 2, result.output


def test_fingerprint_command_resolves_includes(tmp_path: Path) -> None:
    """Editing an included file must change the CLI identifier without changing the root."""
    path = tmp_path / "config.yaml"
    path.write_text("defaults: !include defaults.yaml\n")
    included = tmp_path / "defaults.yaml"
    included.write_text("enable_streaming: false\n")
    first = runner.invoke(app, ["config", "fingerprint", "--path", str(path)])
    included.write_text("enable_streaming: true\n")
    second = runner.invoke(app, ["config", "fingerprint", "--path", str(path)])
    assert first.exit_code == second.exit_code == 0
    assert len(first.stdout.strip()) == 64
    assert first.stdout != second.stdout
