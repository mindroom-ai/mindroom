"""Tests for the local desktop bridge CLI lifecycle."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import nio
import pytest
import typer
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent
from nio.durable import RecordKind, SyncBatch, SyncRecord
from typer.testing import CliRunner

import mindroom.cli.desktop as desktop_cli
from mindroom.cli.desktop import desktop_app
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.protocol import DESKTOP_COMMAND_EVENT_TYPE
from mindroom.desktop.provider import DesktopProviderError
from mindroom.desktop.session import DesktopMatrixSession, save_desktop_session

runner = CliRunner()


def test_native_helper_command_uses_explicit_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CLI native entry shares the same runtime identity as setup and bridge commands."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    activate = MagicMock(return_value=runtime_paths)
    serve = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_activate_desktop_runtime", activate)
    monkeypatch.setattr("mindroom.desktop.native_host.run_native_stdio", serve)
    result = runner.invoke(
        desktop_app,
        ["app", "--config", str(tmp_path / "config.yaml"), "--storage-path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    activate.assert_called_once_with(tmp_path / "config.yaml", storage_path=tmp_path)
    assert serve.await_args.args == (runtime_paths,)


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


def test_desktop_runtime_storage_override_does_not_restore_working_directory_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A storage override keeps the stable user-level Desktop config selection."""
    activate = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        "mindroom.constants.exported_process_env",
        lambda: {"MINDROOM_STORAGE_PATH": "/shared/mindroom-data"},
    )
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", activate)

    desktop_cli._activate_desktop_runtime(None, storage_path=None)

    activate.assert_called_once_with(Path.home() / ".mindroom" / "config.yaml", storage_path=None)


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


@pytest.mark.parametrize("session_exists", [False, True])
def test_desktop_setup_logs_in_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    session_exists: bool,
) -> None:
    """One setup command creates a missing session and always claims pairing."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    if session_exists:
        save_desktop_session(
            session_path,
            DesktopMatrixSession("https://matrix.example.org/", "@alice:example.org", "DESKTOP", "saved-token"),
        )
    login = MagicMock(
        side_effect=lambda **_: save_desktop_session(
            session_path,
            DesktopMatrixSession("https://matrix.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
        ),
    )
    pair = MagicMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr(desktop_cli, "desktop_login", login)
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(
        desktop_app,
        [
            "setup",
            "--allow-agent",
            "computer",
            "--user-id",
            "@alice:example.org",
            "--homeserver",
            "https://matrix.example.org",
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
    assert login.called is not session_exists
    assert pair.call_args.kwargs["code"] == "short-code"


@pytest.mark.parametrize(
    ("saved_homeserver", "saved_user_id"),
    [
        ("https://staging.example.org", "@alice:example.org"),
        ("https://matrix.example.org", "@other:example.org"),
    ],
)
def test_desktop_setup_rejects_saved_session_for_another_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved_homeserver: str,
    saved_user_id: str,
) -> None:
    """A saved session for another homeserver or user cannot silently receive this pairing."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    save_desktop_session(
        session_path,
        DesktopMatrixSession(saved_homeserver, saved_user_id, "DESKTOP", "saved-token"),
    )
    login = MagicMock()
    pair = MagicMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr(desktop_cli, "desktop_login", login)
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(
        desktop_app,
        [
            "setup",
            "--allow-agent",
            "computer",
            "--user-id",
            "@alice:example.org",
            "--homeserver",
            "https://matrix.example.org",
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

    assert result.exit_code == 1
    assert "--storage-path" in result.output
    assert saved_homeserver in result.output
    assert not login.called
    assert not pair.called


def test_desktop_setup_compares_saved_session_with_default_homeserver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without --homeserver, setup compares the saved session with the homeserver login would use."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://staging.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
    )
    pair = MagicMock()
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: runtime_paths)
    monkeypatch.setattr(
        "mindroom.constants.runtime_matrix_homeserver",
        lambda _runtime_paths: "https://matrix.example.org",
    )
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(
        desktop_app,
        [
            "setup",
            "--allow-agent",
            "computer",
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

    assert result.exit_code == 1
    assert "https://staging.example.org" in result.output
    assert not pair.called


@pytest.mark.parametrize("inside_tmux", [False, True])
def test_missing_macos_permission_says_to_restart_the_terminal_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inside_tmux: bool,
) -> None:
    """A granted but not yet applied permission is the common case, so the restart comes first."""
    monkeypatch.setattr("mindroom.desktop.provider.request_macos_desktop_permissions", lambda: ("Accessibility",))
    if inside_tmux:
        monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)

    with pytest.raises(DesktopProviderError) as raised:
        desktop_cli._request_required_desktop_permissions()

    message = str(raised.value)
    assert message.startswith("macOS has not applied Accessibility permission")
    assert "already listed and enabled" in message
    assert "Cmd-Q" in message
    assert ("tmux kill-server" in message) is inside_tmux


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


@pytest.mark.asyncio
async def test_bridge_pins_controller_before_consuming_durable_input(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI attaches durable admission before the transport runs and closes both owners."""
    client = nio.AsyncClient("https://matrix.example.org", config=nio.AsyncClientConfig(encryption_enabled=False))
    lifecycle = []
    admitted = asyncio.Event()
    ready = asyncio.Event()
    ready.set()
    event = AuthenticatedToDeviceEvent(
        source={"content": {}},
        sender="@cloud:example.org",
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice("@cloud:example.org", "CLOUD", "curve", "fingerprint"),
    )
    batch = SyncBatch(uuid4(), 1, (SyncRecord(RecordKind.TO_DEVICE, None, event.source),))

    class Source:
        async def run(self) -> None:
            lifecycle.append("transport")
            await asyncio.Event().wait()

        async def wait_for_work(self) -> None:
            await ready.wait()

        async def next_batch(self) -> SyncBatch:
            ready.clear()
            return batch

        async def dispatch(self, _record: SyncRecord) -> None:
            await client._on_to_device(event)

        async def ack(self, _batch: SyncBatch) -> None:
            assert admitted.is_set()
            lifecycle.append("ack")

    async def close_owner() -> None:
        lifecycle.append("owner_close")
        await client.close()

    owner = SimpleNamespace(client=client, source=Source(), close=close_owner)

    class Bridge:
        async def on_to_device_event(self, _event: object) -> None:
            lifecycle.append("admit")
            admitted.set()

        async def run(self) -> None:
            await admitted.wait()

        async def wait_for_capacity(self) -> None:
            msg = "unexpected capacity pressure"
            raise AssertionError(msg)

        async def stop(self) -> None:
            lifecycle.append("stop")

        def close(self) -> None:
            lifecycle.append("bridge_close")

    async def open_client(*_args: object, **_kwargs: object) -> object:
        lifecycle.append("open")
        return owner

    async def prepare_client(_client: object) -> None:
        assert client.to_device_callbacks
        lifecycle.append("prepare")

    async def resolve_device(*_args: object, **_kwargs: object) -> None:
        lifecycle.append("resolve")

    bridge_options = {}

    def make_bridge(**kwargs: object) -> Bridge:
        bridge_options.update(kwargs)
        return Bridge()

    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.session.prepare_desktop_client", prepare_client)
    monkeypatch.setattr("mindroom.matrix.olm_to_device.resolve_pinned_device", resolve_device)
    monkeypatch.setattr("mindroom.desktop.provider.PyAutoGuiDesktopProvider", lambda **_kwargs: object())
    monkeypatch.setattr(desktop_cli, "_request_required_desktop_permissions", lambda: None)
    monkeypatch.setattr("mindroom.desktop.bridge.DesktopBridge", make_bridge)

    await desktop_cli._run_bridge(
        runtime_paths=SimpleNamespace(storage_root=tmp_path),
        session=DesktopMatrixSession("https://matrix.example.org", "@desktop:example.org", "DESKTOP", "token"),
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

    assert lifecycle[:3] == ["open", "resolve", "prepare"]
    assert lifecycle.index("admit") < lifecycle.index("ack")
    assert lifecycle[-2:] == ["bridge_close", "owner_close"]
    assert client.to_device_callbacks == []
    assert bridge_options["journal_path"] == tmp_path / "desktop_bridge" / "commands.sqlite3"
    assert bridge_options["legacy_journal_path"] == tmp_path / "desktop_bridge" / "command_journal.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["transport_failure", "cancellation"])
async def test_cli_drains_native_work_before_releasing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shutdown: str,
) -> None:
    """Neither transport failure nor cancellation may release ownership over live input."""
    from mindroom.desktop.bridge import DesktopBridge  # noqa: PLC0415

    started = asyncio.Event()
    shutdown_started = asyncio.Event()
    fail_transport = asyncio.Event()
    release_native = threading.Event()
    native_finished = threading.Event()
    owner_closed = False
    client = nio.AsyncClient("https://matrix.example.org", config=nio.AsyncClientConfig(encryption_enabled=False))
    loop = asyncio.get_running_loop()

    def native_action() -> None:
        loop.call_soon_threadsafe(started.set)
        release_native.wait(5)
        native_finished.set()

    class ActiveBridge(DesktopBridge):
        async def run(self) -> None:
            async with self._execution_lock:
                await asyncio.to_thread(native_action)
            await asyncio.Event().wait()

        async def stop(self) -> None:
            shutdown_started.set()
            await super().stop()

    class Source:
        async def run(self) -> None:
            await fail_transport.wait()
            msg = "transport failed"
            raise RuntimeError(msg)

        async def wait_for_work(self) -> None:
            await asyncio.Event().wait()

    async def close_owner() -> None:
        nonlocal owner_closed
        owner_closed = True
        shutdown_started.set()
        await client.close()

    owner = SimpleNamespace(client=client, source=Source(), close=close_owner)
    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", AsyncMock(return_value=owner))
    monkeypatch.setattr("mindroom.desktop.session.prepare_desktop_client", AsyncMock())
    monkeypatch.setattr("mindroom.matrix.olm_to_device.resolve_pinned_device", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.provider.PyAutoGuiDesktopProvider", lambda **_kwargs: object())
    monkeypatch.setattr(desktop_cli, "_request_required_desktop_permissions", lambda: None)
    monkeypatch.setattr("mindroom.desktop.bridge.DesktopBridge", ActiveBridge)
    task = asyncio.create_task(
        desktop_cli._run_bridge(
            runtime_paths=SimpleNamespace(storage_root=tmp_path),
            session=DesktopMatrixSession("https://matrix.example.org", "@desktop:example.org", "DESKTOP", "token"),
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
        ),
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        if shutdown == "cancellation":
            task.cancel()
        else:
            fail_transport.set()
        await asyncio.wait_for(shutdown_started.wait(), 2)
        assert not owner_closed
        assert not native_finished.is_set()
        assert not task.done()
        release_native.set()
        expected = asyncio.CancelledError if shutdown == "cancellation" else RuntimeError
        with pytest.raises(expected):
            await task
        assert native_finished.is_set()
        assert owner_closed
    finally:
        release_native.set()
        await asyncio.gather(task, return_exceptions=True)
