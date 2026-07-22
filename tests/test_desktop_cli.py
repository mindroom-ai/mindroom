"""Tests for the local desktop bridge CLI lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
import typer
from typer.testing import CliRunner

import mindroom.cli.desktop as desktop_cli
from mindroom.cli.desktop import desktop_app
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.provider import DesktopProviderError
from mindroom.desktop.session import DesktopMatrixSession, DesktopSessionError
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

runner = CliRunner()


def test_desktop_runtime_default_is_independent_of_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Local Desktop sessions use one user-level location across shell directories."""
    activate = MagicMock(return_value=SimpleNamespace())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mindroom.constants.exported_process_env", dict)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", activate)

    desktop_cli._activate_desktop_runtime(None, storage_path=None)

    activate.assert_called_once_with(Path.home() / ".mindroom" / "config.yaml", storage_path=None)


def test_desktop_runtime_preserves_explicit_environment_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing runtime environment overrides still select Desktop state."""
    activate = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        "mindroom.constants.exported_process_env",
        lambda: {"MINDROOM_STORAGE_PATH": "/shared/mindroom-data"},
    )
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", activate)

    desktop_cli._activate_desktop_runtime(None, storage_path=None)

    activate.assert_called_once_with(None, storage_path=None)


def test_login_identity_output_routes_users_to_chat_pairing(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Login must not direct users to removed authored device fields."""
    desktop_cli._print_device_identity(
        DesktopMatrixSession(
            homeserver="https://matrix.example.org",
            user_id="@laptop:example.org",
            device_id="LAPTOP",
            access_token="test-token",  # noqa: S106 - Test-only token.
        ),
        fingerprint="fingerprint",
        session_path=tmp_path / "matrix_session.json",
    )

    output = capsys.readouterr().out
    assert "!desktop setup" in output
    assert "cloud agent's desktop tool configuration" not in output


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
    assert login.await_args.kwargs["cloudflare_access"] is False


def test_desktop_login_uses_cloudflare_access_before_matrix_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI authentication reaches login-method discovery and persists its transport mode."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    headers = {"cf-access-token": "access-token"}
    access_headers = MagicMock(return_value=headers)
    discover = AsyncMock(return_value=DesktopLoginMethod.PASSWORD)
    login = AsyncMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.cloudflare_access_headers", access_headers)
    monkeypatch.setattr("mindroom.desktop.session.resolve_desktop_login_method", discover)
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
            "--cloudflare-access",
        ],
    )

    assert result.exit_code == 0, result.output
    access_headers.assert_called_once_with("https://matrix.example.org", None)
    assert discover.await_args.kwargs["http_headers"] is headers
    assert login.await_args.kwargs["http_headers"] is headers
    assert login.await_args.kwargs["cloudflare_access"] is True


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


def test_desktop_pair_sends_claim_with_saved_local_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The setup command can present its token from the already logged-in local device."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    session = SimpleNamespace(homeserver="https://matrix.example.org", cloudflare_access=False)
    pair = AsyncMock(return_value="VERIFY123")
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", lambda _path: session)
    monkeypatch.setattr(desktop_cli, "_pair_desktop", pair)

    result = runner.invoke(
        desktop_app,
        [
            "pair",
            "--code",
            "short-code",
            "--controller-user-id",
            "@computer:example.org",
            "--controller-device-id",
            "CLOUD",
            "--controller-ed25519",
            "cloud-fingerprint",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pair.await_args.kwargs["session"] is session
    assert pair.await_args.kwargs["code"] == "short-code"
    assert pair.await_args.kwargs["controller_user_id"] == "@computer:example.org"
    assert "!desktop confirm short-code VERIFY123" in result.output


def test_desktop_run_loads_matrix_http_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Long-running sync receives the same proxy headers as one-time login."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    headers_path = tmp_path / "matrix-http-headers.json"
    headers_path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    headers_path.chmod(0o600)
    bridge = AsyncMock()
    ensure_dependencies = MagicMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(desktop_cli, "_ensure_desktop_dependencies", ensure_dependencies)
    monkeypatch.setattr(
        "mindroom.desktop.session.load_desktop_session",
        lambda _path: SimpleNamespace(homeserver="https://matrix.example.org", cloudflare_access=False),
    )
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
    ensure_dependencies.assert_called_once_with(runtime_paths)
    assert bridge.await_args.kwargs["http_headers"] == {"X-Access-Client": "test-secret"}


def test_desktop_run_restores_saved_cloudflare_access_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One-time login choice automatically applies to later bridge runs."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    session = SimpleNamespace(homeserver="https://matrix.example.org", cloudflare_access=True)
    headers = MagicMock()
    access_headers = MagicMock(return_value=headers)
    bridge = AsyncMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(desktop_cli, "_ensure_desktop_dependencies", MagicMock())
    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", lambda _path: session)
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.cloudflare_access_headers", access_headers)
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
        ],
    )

    assert result.exit_code == 0, result.output
    access_headers.assert_called_once_with("https://matrix.example.org", None)
    assert bridge.await_args.kwargs["http_headers"] is headers


def test_desktop_dependencies_use_optional_extra_auto_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop startup reuses optional-extra installation, including macOS frameworks."""
    ensure = MagicMock()
    runtime_paths = SimpleNamespace()
    monkeypatch.setattr(desktop_cli.sys, "platform", "darwin")
    monkeypatch.setattr("mindroom.tool_system.dependencies.ensure_optional_deps", ensure)

    desktop_cli._ensure_desktop_dependencies(runtime_paths)

    ensure.assert_called_once_with(
        [
            "pyautogui",
            "pyobjc-framework-applicationservices",
            "pyobjc-framework-cocoa",
        ],
        "desktop",
        runtime_paths,
    )


def test_desktop_dependency_install_failure_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled or failed auto-install keeps the existing desktop-domain CLI error."""
    monkeypatch.setattr(desktop_cli.sys, "platform", "linux")
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies.ensure_optional_deps",
        MagicMock(side_effect=ImportError("install mindroom[desktop]")),
    )

    with pytest.raises(DesktopProviderError, match=r"mindroom\[desktop\]"):
        desktop_cli._ensure_desktop_dependencies(SimpleNamespace())


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
