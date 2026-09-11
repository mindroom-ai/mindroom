"""The config receipt CLI must match the requested source before returning success."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from mindroom.cli.config import activate_cli_runtime
from mindroom.cli.main import app
from mindroom.config.main import load_config

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.mark.parametrize(
    ("url", "expected_proxy"),
    [("http://127.0.0.1:8765", False), ("https://example.org", True)],
)
def test_operator_key_uses_proxies_only_over_https(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected_proxy: bool,
) -> None:
    """Local plaintext credentials stay direct; HTTPS retains configured proxy support."""
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "")
    routed_through_proxy: list[bool] = []

    class Transport(httpx.MockTransport):
        """Replace network I/O while preserving HTTPX's proxy selection."""

        def __init__(self, *, proxy: httpx.Proxy | None = None, **_kwargs: object) -> None:
            def respond(_request: httpx.Request) -> httpx.Response:
                routed_through_proxy.append(proxy is not None)
                return httpx.Response(200, json={"status": "applied", "fingerprint": "a" * 64})

            super().__init__(respond)

    monkeypatch.setattr("httpx._client.HTTPTransport", Transport)
    result = runner.invoke(app, ["config", "check-applied", "--fingerprint", "a" * 64, "--url", url])
    assert result.exit_code == 0, result.output
    assert routed_through_proxy == [expected_proxy]


@pytest.mark.parametrize("source", ["- item\n", "42\n"])
def test_non_mapping_source_returns_json_error(tmp_path: Path, source: str) -> None:
    """Nonempty scalar/list YAML roots must not escape the CLI error boundary."""
    path = tmp_path / "config.yaml"
    path.write_text(source)
    result = runner.invoke(app, ["config", "check-applied", "--path", str(path), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


@pytest.mark.parametrize("command", ["fingerprint", "check-applied"])
def test_legacy_access_requires_migration_before_fingerprinting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Automatic migration rewrites source bytes, so capture a target only after migration."""
    path = tmp_path / "config.yaml"
    source = "authorization: {global_users: ['@alice:localhost']}\n"
    path.write_text(source)
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: pytest.fail("Unexpected HTTP request"))
    result = runner.invoke(app, ["config", command, "--path", str(path)])
    assert result.exit_code == 2
    assert "config migrate" in result.output
    assert path.read_text() == source
    assert list(tmp_path.iterdir()) == [path]


def test_explicitly_migrated_source_matches_runtime_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration command produces stable bytes for subsequent confirmation."""
    path = tmp_path / "config.yaml"
    path.write_text("authorization: {global_users: ['@alice:localhost']}\n")
    migrated = runner.invoke(app, ["config", "migrate", "--path", str(path)])
    assert migrated.exit_code == 0, migrated.output
    loaded = load_config(activate_cli_runtime(path))
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={"status": "applied", "fingerprint": loaded.source_fingerprint},
        ),
    )
    fingerprint = runner.invoke(app, ["config", "fingerprint", "--path", str(path)])
    assert fingerprint.exit_code == 0, fingerprint.output
    assert fingerprint.stdout.strip() == loaded.source_fingerprint
    confirmed = runner.invoke(app, ["config", "check-applied", "--path", str(path)])
    assert confirmed.exit_code == 0, confirmed.output


@pytest.mark.parametrize(("timeout_at", "expected_exit"), [(2.0, 1), (1.5, 2)])
def test_http_timeout_at_wait_deadline_preserves_pending(
    monkeypatch: pytest.MonkeyPatch,
    timeout_at: float,
    expected_exit: int,
) -> None:
    """Exhausting a known-pending wait differs from losing contact before its deadline."""
    now = [0.0]

    def get(*_args: object, **_kwargs: object) -> httpx.Response:
        if now[0] == 0:
            return httpx.Response(200, json={"status": "pending", "fingerprint": "a" * 64})
        now[0] = timeout_at
        msg = "read timed out"
        raise httpx.ReadTimeout(msg)

    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr("mindroom.cli.config_reload.time.monotonic", lambda: now[0])
    monkeypatch.setattr("mindroom.cli.config_reload.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    result = runner.invoke(app, ["config", "check-applied", "--fingerprint", "a" * 64, "--wait", "2", "--json"])
    assert result.exit_code == expected_exit, result.output
    assert json.loads(result.stdout)["status"] == ("pending" if expected_exit == 1 else "unavailable")


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
