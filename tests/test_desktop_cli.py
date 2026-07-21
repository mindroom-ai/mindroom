"""Tests for the local desktop bridge CLI lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
import typer
from typer.testing import CliRunner

import mindroom.cli.desktop as desktop_cli
from mindroom.cli.desktop import desktop_app
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.session import DesktopMatrixSession, DesktopSessionError
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


def test_desktop_login_accepts_explicit_homeserver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh local machine can target cloud Matrix without hidden environment setup."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    headers_path = tmp_path / "matrix-http-headers.json"
    headers_path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    headers_path.chmod(0o600)
    login = AsyncMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr(desktop_cli, "_login_and_save", login)
    monkeypatch.setenv("MINDROOM_DESKTOP_MATRIX_PASSWORD", "test-password")

    result = runner.invoke(
        desktop_app,
        [
            "login",
            "--user-id",
            "@laptop:example.org",
            "--homeserver",
            "https://matrix.example.org",
            "--login-method",
            "password",
            "--matrix-http-headers-file",
            str(headers_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert login.await_args.kwargs["homeserver"] == "https://matrix.example.org"
    assert login.await_args.kwargs["http_headers"] == {"X-Access-Client": "test-secret"}
    assert login.await_args.kwargs["password"] == "test-password"  # noqa: S105 - Test-only password.
    assert login.await_args.kwargs["login_token"] is None


def test_desktop_login_uses_browser_sso_without_password_or_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SSO-only homeservers open a browser and persist the returned Matrix session."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    login = AsyncMock()
    discover = AsyncMock(return_value=DesktopLoginMethod.SSO)

    def receive_token(*_args: object, **_kwargs: object) -> str:
        return "short-lived-token"

    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.desktop.session.resolve_desktop_login_method", discover)
    monkeypatch.setattr("mindroom.desktop.sso.receive_sso_login_token", receive_token)
    monkeypatch.setattr(desktop_cli, "_login_and_save", login)

    result = runner.invoke(
        desktop_app,
        ["login", "--homeserver", "https://matrix.example.org"],
    )

    assert result.exit_code == 0, result.output
    discover.assert_awaited_once()
    assert login.await_args.kwargs["user_id"] is None
    assert login.await_args.kwargs["password"] is None
    assert login.await_args.kwargs["login_token"] == "short-lived-token"  # noqa: S105 - Test-only token.


def test_desktop_login_sso_idp_selects_sso_and_reaches_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A named IdP selects SSO and is forwarded to the browser redirect."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    login = AsyncMock()
    discover = AsyncMock(return_value=DesktopLoginMethod.SSO)
    receive_token = MagicMock(return_value="short-lived-token")
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.desktop.session.resolve_desktop_login_method", discover)
    monkeypatch.setattr("mindroom.desktop.sso.receive_sso_login_token", receive_token)
    monkeypatch.setattr(desktop_cli, "_login_and_save", login)

    result = runner.invoke(
        desktop_app,
        ["login", "--homeserver", "https://matrix.example.org", "--sso-idp", "company-sso"],
    )

    assert result.exit_code == 0, result.output
    assert discover.await_args.args[0] is DesktopLoginMethod.SSO
    assert receive_token.call_args.kwargs["idp_id"] == "company-sso"


def test_desktop_password_login_requires_user_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit password mode fails before prompting when its identity is missing."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)

    result = runner.invoke(
        desktop_app,
        ["login", "--homeserver", "https://matrix.example.org", "--login-method", "password"],
    )

    assert result.exit_code == 1
    assert "--user-id is required" in result.output


def test_desktop_run_loads_matrix_http_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Long-running sync receives the same proxy headers as one-time login."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    headers_path = tmp_path / "matrix-http-headers.json"
    headers_path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    headers_path.chmod(0o600)
    bridge = AsyncMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", lambda _path: object())
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = runner.invoke(
        desktop_app,
        [
            "run",
            "--controller-user-id",
            "@cloud:example.org",
            "--controller-device-id",
            "CLOUD",
            "--controller-ed25519",
            "fingerprint",
            "--allow-requester",
            "@alice:example.org",
            "--allow-agent",
            "computer",
            "--allow-app",
            "com.example.Editor",
            "--matrix-http-headers-file",
            str(headers_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert bridge.await_args.kwargs["http_headers"] == {"X-Access-Client": "test-secret"}


def test_browser_profile_paths_require_extension_mode(tmp_path: Path) -> None:
    """Profile options cannot be silently ignored when extension mode is absent."""
    with pytest.raises(typer.Exit) as exc_info:
        desktop_cli._validate_browser_options(
            enabled=False,
            executable_path=tmp_path / "Brave",
            user_data_dir=None,
        )

    assert exc_info.value.exit_code == 2


def test_browser_profile_paths_must_exist(tmp_path: Path) -> None:
    """Bad local browser paths fail before Matrix login and sync startup."""
    with pytest.raises(typer.Exit) as exc_info:
        desktop_cli._validate_browser_options(
            enabled=True,
            executable_path=tmp_path / "missing-brave",
            user_data_dir=None,
        )

    assert exc_info.value.exit_code == 2


def test_controller_command_preserves_unexpected_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Filesystem failures retain their traceback instead of becoming a generic CLI exit."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)

    def denied(*_args: object, **_kwargs: object) -> None:
        message = "test identity permission failure"
        raise PermissionError(message)

    monkeypatch.setattr("mindroom.desktop.identity.controller_identity_for_entity", denied)

    result = runner.invoke(desktop_app, ["controller", "--entity", "computer"])

    assert isinstance(result.exception, PermissionError)


def test_login_command_preserves_unexpected_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Session persistence failures remain distinguishable from expected login errors."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr(desktop_cli, "_login_and_save", AsyncMock(side_effect=PermissionError("test write failure")))
    monkeypatch.setenv("MINDROOM_DESKTOP_MATRIX_PASSWORD", "test-password")

    result = runner.invoke(
        desktop_app,
        [
            "login",
            "--user-id",
            "@laptop:example.org",
            "--homeserver",
            "https://matrix.example.org",
            "--login-method",
            "password",
        ],
    )

    assert isinstance(result.exception, PermissionError)


def test_run_command_preserves_unexpected_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected session I/O errors are not flattened into a friendly domain failure."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)

    def denied(_path: Path) -> None:
        message = "test session permission failure"
        raise PermissionError(message)

    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", denied)

    result = runner.invoke(
        desktop_app,
        [
            "run",
            "--controller-user-id",
            "@cloud:example.org",
            "--controller-device-id",
            "CLOUD",
            "--controller-ed25519",
            "fingerprint",
            "--allow-requester",
            "@alice:example.org",
            "--allow-agent",
            "computer",
            "--allow-app",
            "com.example.Editor",
        ],
    )

    assert isinstance(result.exception, PermissionError)


class _FakeBridgeClient:
    def __init__(self) -> None:
        self.to_device_callback: object | None = None
        self.response_callback: object | None = None
        self.sync_error: nio.SyncError | None = None
        self.stopped = False

    def add_to_device_callback(self, callback: object, _event_type: object) -> None:
        self.to_device_callback = callback

    def add_response_callback(self, callback: object, _response_type: object) -> None:
        self.response_callback = callback

    async def sync_forever(self, **_kwargs: object) -> None:
        if self.sync_error is not None and self.response_callback is not None:
            await self.response_callback(self.sync_error)  # type: ignore[operator]

    def stop_sync_forever(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_bridge_pins_controller_before_consuming_initial_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh stores can authenticate queued commands before the initial sync acknowledges them."""
    client = _FakeBridgeClient()
    bridge = SimpleNamespace(on_to_device_event=AsyncMock())
    lifecycle: list[str] = []
    controller_resolved = False

    async def open_client(*_args: object, **_kwargs: object) -> _FakeBridgeClient:
        lifecycle.append("open")
        return client

    async def prepare_client(preparing_client: _FakeBridgeClient) -> None:
        lifecycle.append("prepare")
        assert controller_resolved
        assert preparing_client.to_device_callback is not None
        event = AuthenticatedToDeviceEvent(
            source={"content": {}},
            sender="@cloud:example.org",
            type="io.mindroom.desktop.command.v1",
            authenticated_device_id="CLOUD",
        )
        preparing_client.to_device_callback(event)  # type: ignore[operator]
        await asyncio.sleep(0)

    async def resolve_device(*_args: object, **_kwargs: object) -> None:
        nonlocal controller_resolved
        lifecycle.append("resolve")
        controller_resolved = True

    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.session.prepare_desktop_client", prepare_client)
    monkeypatch.setattr("mindroom.matrix.olm_to_device.resolve_pinned_device", resolve_device)
    monkeypatch.setattr("mindroom.desktop.provider.PyAutoGuiDesktopProvider", lambda **_kwargs: object())
    monkeypatch.setattr("mindroom.desktop.bridge.DesktopBridge", lambda **_kwargs: bridge)

    await desktop_cli._run_bridge(
        runtime_paths=SimpleNamespace(storage_root=tmp_path),
        session=DesktopMatrixSession(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            device_id="DESKTOP",
            access_token="token",  # noqa: S106 - Test-only Matrix session fixture.
        ),
        controller_user_id="@cloud:example.org",
        controller_device_id="CLOUD",
        controller_ed25519="fingerprint",
        allow_requester=frozenset({"@alice:example.org"}),
        allow_agent=frozenset({"computer"}),
        allow_app=frozenset({"com.example.Editor"}),
        allow_control=False,
        lease_minutes=15,
        max_screenshot_width=1600,
        jpeg_quality=80,
    )

    assert lifecycle == ["open", "resolve", "prepare"]
    bridge.on_to_device_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_sync_error_stops_bridge_with_clear_failure() -> None:
    """A revoked desktop token exits instead of spinning under an online banner."""
    client = _FakeBridgeClient()
    client.sync_error = nio.SyncError("Access token revoked", status_code="M_UNKNOWN_TOKEN")

    with pytest.raises(DesktopSessionError, match="permanent authentication failure"):
        await desktop_cli._sync_desktop_client(client)

    assert client.stopped
