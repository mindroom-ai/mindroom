"""Tests for the external trigger CLI."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import cast

import httpx
import pytest
from click.utils import strip_ansi
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.external_triggers.auth import verify_trigger_request

runner = CliRunner()


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _write_private_key(path: Path) -> Ed25519PrivateKey:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    path.write_text(base64.b64encode(raw).decode("ascii"), encoding="utf-8")
    return private_key


def test_trigger_keygen_writes_private_key_and_prints_public_key(tmp_path: Path) -> None:
    """Keygen should write a raw Ed25519 private key and print the derived public key."""
    key_path = tmp_path / "trigger.key"

    result = runner.invoke(app, ["trigger", "keygen", "--private-key-file", str(key_path)])

    assert result.exit_code == 0
    raw_private_key = base64.b64decode(key_path.read_text(encoding="utf-8"), validate=True)
    assert len(raw_private_key) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600

    private_key = Ed25519PrivateKey.from_private_bytes(raw_private_key)
    expected_public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert f"private_key_file={key_path}" in result.output
    assert f"public_key={base64.b64encode(expected_public_key).decode('ascii')}" in result.output


def test_trigger_keygen_does_not_create_group_readable_file_before_chmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keygen should not rely on chmod to narrow permissive umask-created files."""
    key_path = tmp_path / "trigger.key"
    modes_before_chmod: list[int] = []
    original_chmod = Path.chmod

    def tracking_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == key_path and self.exists():
            modes_before_chmod.append(self.stat().st_mode & 0o777)
        original_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", tracking_chmod)
    previous_umask = os.umask(0o022)
    try:
        result = runner.invoke(app, ["trigger", "keygen", "--private-key-file", str(key_path)])
    finally:
        os.umask(previous_umask)

    assert result.exit_code == 0
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert all(mode == 0o600 for mode in modes_before_chmod), modes_before_chmod


def test_trigger_keygen_restricts_existing_private_key_file(tmp_path: Path) -> None:
    """Keygen should keep existing private key files at mode 0600 after overwrite."""
    key_path = tmp_path / "trigger.key"
    key_path.write_text("old-key", encoding="utf-8")
    key_path.chmod(0o644)

    result = runner.invoke(app, ["trigger", "keygen", "--private-key-file", str(key_path)])

    assert result.exit_code == 0
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.read_text(encoding="utf-8") != "old-key"


def test_trigger_keygen_rejects_directory_without_changing_permissions(tmp_path: Path) -> None:
    """Keygen should reject directory targets without changing directory permissions."""
    key_path = tmp_path / "trigger-dir"
    key_path.mkdir()
    key_path.chmod(0o700)

    result = runner.invoke(app, ["trigger", "keygen", "--private-key-file", str(key_path)])
    mode_after = key_path.stat().st_mode & 0o777
    key_path.chmod(0o700)

    assert result.exit_code == 2
    assert "regular file" in result.output
    assert mode_after == 0o700


def test_trigger_keygen_rejects_symlink_without_changing_target(tmp_path: Path) -> None:
    """Keygen should reject symlink targets without overwriting the linked file."""
    target_path = tmp_path / "target.txt"
    target_path.write_text("do-not-overwrite", encoding="utf-8")
    key_path = tmp_path / "trigger.key"
    try:
        key_path.symlink_to(target_path)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = runner.invoke(app, ["trigger", "keygen", "--private-key-file", str(key_path)])

    assert result.exit_code == 2
    assert "regular file" in result.output
    assert key_path.is_symlink()
    assert target_path.read_text(encoding="utf-8") == "do-not-overwrite"


def test_trigger_send_builds_default_signed_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Send should build compact JSON, sign it, and use documented defaults."""
    key_path = tmp_path / "trigger.key"
    private_key = _write_private_key(key_path)
    public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(
        "ascii",
    )
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: float,
        verify: bool,
    ) -> _FakeResponse:
        captured.update(url=url, content=content, headers=headers, timeout=timeout, verify=verify)
        return _FakeResponse(
            {
                "accepted": True,
                "duplicate": False,
                "trigger_id": "campground",
                "event_id": "generated-event",
            },
        )

    monkeypatch.setattr("mindroom.cli.trigger.httpx.post", fake_post)
    monkeypatch.setattr("mindroom.cli.trigger.time.time", lambda: 1234.9)
    token_values = iter(["generated-event", "nonce-1"])
    monkeypatch.setattr("mindroom.cli.trigger.secrets.token_hex", lambda _size: next(token_values))

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--key-file",
            str(key_path),
            "--kind",
            "campground.availability",
            "--message",
            "site open",
        ],
    )

    assert result.exit_code == 0
    assert captured["url"] == "http://127.0.0.1:8765/api/triggers/campground"
    assert captured["content"] == (
        b'{"kind":"campground.availability","message":"site open","event_id":"generated-event","data":{}}'
    )
    assert captured["timeout"] == 10.0
    assert captured["verify"] is True
    content = cast("bytes", captured["content"])
    headers = cast("dict[str, str]", captured["headers"])
    verify_trigger_request(
        method="POST",
        path="/api/triggers/campground",
        body=content,
        headers=headers,
        expected_key_id="default",
        public_key_b64=public_key_b64,
        now=1234,
    )
    assert '"accepted": true' in result.output


def test_trigger_send_accepts_custom_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Send should honor optional URL, key id, TLS, timeout, title, data, and event id values."""
    key_path = tmp_path / "trigger.key"
    private_key = _write_private_key(key_path)
    public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(
        "ascii",
    )
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: float,
        verify: bool,
    ) -> _FakeResponse:
        captured.update(url=url, content=content, headers=headers, timeout=timeout, verify=verify)
        return _FakeResponse({"accepted": True})

    monkeypatch.setattr("mindroom.cli.trigger.httpx.post", fake_post)
    monkeypatch.setattr("mindroom.cli.trigger.time.time", lambda: 2000)
    monkeypatch.setattr("mindroom.cli.trigger.secrets.token_hex", lambda _size: "nonce-2")

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--url",
            "https://example.test/prefix/",
            "--key-file",
            str(key_path),
            "--key-id",
            "rotated",
            "--kind",
            "campground.availability",
            "--message",
            "site open",
            "--event-id",
            "event-1",
            "--title",
            "Site open",
            "--data-json",
            '{"site":42}',
            "--timeout",
            "2.5",
            "--no-verify-tls",
        ],
    )

    assert result.exit_code == 0
    assert captured["url"] == "https://example.test/prefix/api/triggers/campground"
    content = cast("bytes", captured["content"])
    headers = cast("dict[str, str]", captured["headers"])
    assert json.loads(content) == {
        "kind": "campground.availability",
        "message": "site open",
        "event_id": "event-1",
        "title": "Site open",
        "data": {"site": 42},
    }
    assert b": " not in content
    assert b", " not in content
    assert captured["timeout"] == 2.5
    assert captured["verify"] is False
    verify_trigger_request(
        method="POST",
        path="/prefix/api/triggers/campground",
        body=content,
        headers=headers,
        expected_key_id="rotated",
        public_key_b64=public_key_b64,
        now=2000,
    )


def test_trigger_send_reports_transport_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Send should turn network failures into concise CLI errors."""
    key_path = tmp_path / "trigger.key"
    _write_private_key(key_path)

    def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        message = "connection refused"
        raise httpx.ConnectError(message)

    monkeypatch.setattr("mindroom.cli.trigger.httpx.post", fake_post)

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--key-file",
            str(key_path),
            "--kind",
            "campground.availability",
            "--message",
            "site open",
        ],
    )

    assert result.exit_code == 1
    assert "external trigger request failed" in result.output
    assert "connection refused" in result.output


def test_trigger_send_reports_status_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Send should report server status errors without a traceback."""
    key_path = tmp_path / "trigger.key"
    _write_private_key(key_path)
    request = httpx.Request("POST", "http://127.0.0.1:8765/api/triggers/campground")
    response = httpx.Response(
        503,
        request=request,
        json={"detail": "External trigger runtime is not available"},
    )

    def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr("mindroom.cli.trigger.httpx.post", fake_post)

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--key-file",
            str(key_path),
            "--kind",
            "campground.availability",
            "--message",
            "site open",
        ],
    )

    assert result.exit_code == 1
    assert "HTTP 503" in result.output
    assert "External trigger runtime is" in result.output
    assert "not available" in result.output


def test_trigger_send_rejects_malformed_private_key(tmp_path: Path) -> None:
    """Send should reject private key files that are not base64 raw 32-byte Ed25519 keys."""
    key_path = tmp_path / "trigger.key"
    key_path.write_text(base64.b64encode(b"too short").decode("ascii"), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--key-file",
            str(key_path),
            "--kind",
            "campground.availability",
            "--message",
            "site open",
        ],
    )

    assert result.exit_code == 2
    assert "raw 32-byte Ed25519 private key" in result.output


def test_trigger_send_rejects_data_json_that_is_not_object(tmp_path: Path) -> None:
    """Send should reject data-json values that decode to non-object JSON."""
    key_path = tmp_path / "trigger.key"
    _write_private_key(key_path)

    result = runner.invoke(
        app,
        [
            "trigger",
            "send",
            "campground",
            "--key-file",
            str(key_path),
            "--kind",
            "campground.availability",
            "--message",
            "site open",
            "--data-json",
            "[]",
        ],
        env={"FORCE_COLOR": "1"},
    )

    assert result.exit_code == 2
    assert "--data-json must decode to a JSON object" in strip_ansi(result.output)
