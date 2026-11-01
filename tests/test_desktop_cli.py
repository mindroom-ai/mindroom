"""Tests for the local desktop bridge CLI lifecycle."""

from __future__ import annotations

import asyncio
import stat
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from typer.testing import CliRunner

import mindroom.cli.desktop as desktop_cli
from mindroom.cli.desktop import desktop_app
from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.bridge_session import DesktopBridgeSessionDeps, DesktopWorkerStoppedError
from mindroom.desktop.command_journal import DesktopCommandJournal, controller_journal_key, desktop_journal_path
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)
from mindroom.desktop.provider import DesktopProviderError
from mindroom.desktop.session import DesktopMatrixSession, save_desktop_session
from mindroom.desktop.shell import DesktopShellError
from mindroom.matrix.device_identity import PinnedMatrixDevice
from tests.desktop_helpers import (
    SAVED_SESSION,
    FailingSource,
    FakeMatrixOwner,
    offline_session_deps,
    runtime_paths,
)
from tests.desktop_helpers import empty_selected_root as empty_selected_root  # noqa: PLC0414
from tests.desktop_helpers import login_environment as login_environment  # noqa: PLC0414

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import nio

runner = CliRunner()
_RUN_ARGS = [
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
]
_PAIRING_ARGS = [
    "--code",
    "short-code",
    "--controller-user-id",
    "@computer:example.org",
    "--controller-device-id",
    "CLOUD",
    "--controller-ed25519",
    "cloud-fingerprint",
]
_SETUP_ARGS = [
    "setup",
    "--allow-agent",
    "computer",
    "--user-id",
    "@alice:example.org",
    "--homeserver",
    "https://matrix.example.org",
    *_PAIRING_ARGS,
]


@pytest.fixture
def cli_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Activate a CLI runtime rooted at `tmp_path`."""
    paths = SimpleNamespace(storage_root=tmp_path)
    monkeypatch.setattr("mindroom.cli.config.activate_cli_runtime", lambda *_args, **_kwargs: paths)
    return paths


def _stub_run_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, cloudflare_access: bool = False) -> MagicMock:
    """Stub what `desktop run` needs before the bridge starts, and return the dependency installer."""
    ensure_dependencies = MagicMock()
    monkeypatch.setattr(
        "mindroom.cli.config.activate_cli_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(storage_root=tmp_path),
    )
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(desktop_cli, "_ensure_desktop_dependencies", ensure_dependencies)
    monkeypatch.setattr(
        "mindroom.desktop.session.load_desktop_session",
        lambda _path: SimpleNamespace(homeserver="https://matrix.example.org", cloudflare_access=cloudflare_access),
    )
    return ensure_dependencies


def _run_config(
    *,
    roots: tuple[Path, ...],
    apps: tuple[str, ...] = (),
    shell_enabled: bool = False,
) -> NativeDesktopConfig:
    """Return the resolved configuration a terminal run passes to the bridge."""
    return NativeDesktopConfig(
        revision=1,
        enabled=True,
        controller=PinnedMatrixDevice("@cloud:example.org", "CLOUD", "fingerprint"),
        allowed_requester_ids=("@alice:example.org",),
        allowed_agent_names=("computer",),
        allowed_app_ids=apps,
        capture=NativeCaptureConfig(max_screenshot_width=1600, jpeg_quality=80),
        browser=NativeBrowserConfig(),
        files=NativeFilesConfig(roots),
        shell=NativeShellConfig(enabled=shell_enabled),
    )


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


@pytest.mark.parametrize(
    ("chdir", "environment"),
    [(True, {}), (False, {"MINDROOM_STORAGE_PATH": "/shared/mindroom-data"})],
    ids=["other_working_directory", "storage_override"],
)
def test_desktop_runtime_default_is_the_user_level_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    chdir: bool,
    environment: dict[str, str],
) -> None:
    """Local Desktop sessions use one user-level config across shell directories, even with a storage override."""
    activate = MagicMock(return_value=SimpleNamespace())
    if chdir:
        monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mindroom.constants.exported_process_env", lambda: dict(environment))
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


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_login_accepts_explicit_homeserver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh local machine can target cloud Matrix without hidden environment setup."""
    headers_path = tmp_path / "matrix-http-headers.json"
    headers_path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    headers_path.chmod(0o600)
    login = AsyncMock()
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


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_login_uses_cloudflare_access_before_matrix_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI authentication reaches login-method discovery and persists its transport mode."""
    headers = {"cf-access-token": "access-token"}
    access_headers = MagicMock(return_value=headers)
    discover = AsyncMock(return_value=DesktopLoginMethod.PASSWORD)
    login = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.session.desktop_http_headers", access_headers)
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
    access_headers.assert_called_once_with("https://matrix.example.org", None, cloudflare_access=True)
    assert discover.await_args.kwargs["http_headers"] is headers
    assert login.await_args.kwargs["http_headers"] is headers
    assert login.await_args.kwargs["cloudflare_access"] is True


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_login_uses_browser_sso_without_password_or_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSO-only homeservers open a browser and persist the returned Matrix session."""
    login = AsyncMock()
    discover = AsyncMock(return_value=DesktopLoginMethod.SSO)
    receive_token = MagicMock(return_value="short-lived-token")
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


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_login_sso_idp_selects_sso_and_reaches_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named IdP selects SSO and is forwarded to the browser redirect."""
    login = AsyncMock()
    discover = AsyncMock(return_value=DesktopLoginMethod.SSO)
    receive_token = MagicMock(return_value="short-lived-token")
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


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_password_login_requires_user_id() -> None:
    """Explicit password mode fails before prompting when its identity is missing."""
    result = runner.invoke(
        desktop_app,
        ["login", "--homeserver", "https://matrix.example.org", "--login-method", "password"],
    )

    assert result.exit_code == 1
    assert "--user-id is required" in result.output


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_pair_sends_claim_with_saved_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup command can present its token from the already logged-in local device."""
    session = SimpleNamespace(homeserver="https://matrix.example.org", cloudflare_access=False)
    pair = AsyncMock(return_value="VERIFY123")
    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", lambda _path: session)
    monkeypatch.setattr(desktop_cli, "_pair_desktop", pair)

    result = runner.invoke(desktop_app, ["pair", *_PAIRING_ARGS])

    assert result.exit_code == 0, result.output
    assert pair.await_args.kwargs["session"] is session
    assert pair.await_args.kwargs["code"] == "short-code"
    assert pair.await_args.kwargs["controller_user_id"] == "@computer:example.org"
    assert "!desktop confirm short-code VERIFY123" in result.output


@pytest.mark.parametrize("session_exists", [False, True])
@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_logs_in_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    session_exists: bool,
) -> None:
    """One setup command creates a missing session and always claims pairing."""
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
    monkeypatch.setattr(desktop_cli, "desktop_login", login)
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(desktop_app, _SETUP_ARGS)

    assert result.exit_code == 0, result.output
    assert login.called is not session_exists
    assert pair.call_args.kwargs["code"] == "short-code"


@pytest.mark.parametrize(("content", "mode", "saved_revision"), [("{", 0o600, 0), (None, 0o644, 1)])
@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_repairs_malformed_or_exposed_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str | None,
    mode: int,
    saved_revision: int,
) -> None:
    """Setup saves fresh settings over a file the load error says saving can repair."""
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://matrix.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
    )
    path = native_config_path(tmp_path)
    if content is None:
        save_native_config(path, replace(_run_config(roots=()), revision=0), expected_revision=0)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    monkeypatch.setattr(desktop_cli, "desktop_pair", MagicMock())

    result = runner.invoke(desktop_app, _SETUP_ARGS)

    assert result.exit_code == 0, result.output
    saved = load_native_config(path)
    assert saved.revision == saved_revision + 1
    assert saved.controller == PinnedMatrixDevice("@computer:example.org", "CLOUD", "cloud-fingerprint")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(("journal_device_id", "accepted"), [("CLOUD", True), ("OTHER", False)])
@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_pairs_only_the_controller_the_bridge_journal_is_bound_to(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    journal_device_id: str,
    *,
    accepted: bool,
) -> None:
    """Setup refuses, before pairing, a controller that the existing command journal would reject at run time."""
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://matrix.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
    )
    DesktopCommandJournal.load(
        desktop_journal_path(tmp_path),
        controller_key=controller_journal_key(
            PinnedMatrixDevice("@computer:example.org", journal_device_id, "cloud-fingerprint"),
        ),
    ).close()
    pair = MagicMock()
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(desktop_app, _SETUP_ARGS)

    assert result.exit_code == (0 if accepted else 1), result.output
    assert pair.called is accepted
    assert ("different controller" in result.output) is not accepted
    assert native_config_path(tmp_path).exists() is accepted


@pytest.mark.parametrize("shell_enabled", [False, True])
@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_asks_for_access_while_only_a_browser_is_saved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    shell_enabled: bool,
) -> None:
    """Browser control needs a saved app, so setup asks for access until apps, folders, or shell are saved."""
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://matrix.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
    )
    saved = replace(
        _run_config(roots=(), shell_enabled=shell_enabled),
        revision=0,
        controller=PinnedMatrixDevice("@computer:example.org", "CLOUD", "cloud-fingerprint"),
        browser=NativeBrowserConfig(enabled=True),
    )
    save_native_config(native_config_path(tmp_path), saved, expected_revision=0)
    monkeypatch.setattr(desktop_cli, "desktop_pair", MagicMock())

    result = runner.invoke(desktop_app, _SETUP_ARGS)

    assert result.exit_code == 0, result.output
    assert load_native_config(native_config_path(tmp_path)).shell.enabled is shell_enabled
    assert ("open Computer access" in result.output) is not shell_enabled


@pytest.mark.parametrize(
    ("saved_homeserver", "saved_user_id"),
    [
        ("https://staging.example.org", "@alice:example.org"),
        ("https://matrix.example.org", "@other:example.org"),
    ],
)
@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_rejects_saved_session_for_another_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved_homeserver: str,
    saved_user_id: str,
) -> None:
    """A saved session for another homeserver or user cannot silently receive this pairing."""
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    save_desktop_session(
        session_path,
        DesktopMatrixSession(saved_homeserver, saved_user_id, "DESKTOP", "saved-token"),
    )
    login = MagicMock()
    pair = MagicMock()
    monkeypatch.setattr(desktop_cli, "desktop_login", login)
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(desktop_app, _SETUP_ARGS)

    assert result.exit_code == 1
    assert "--storage-path" in result.output
    assert saved_homeserver in result.output
    assert not login.called
    assert not pair.called


@pytest.mark.usefixtures("cli_runtime")
def test_desktop_setup_compares_saved_session_with_default_homeserver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without --homeserver, setup compares the saved session with the homeserver login would use."""
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://staging.example.org", "@alice:example.org", "DESKTOP", "saved-token"),
    )
    pair = MagicMock()
    monkeypatch.setattr(
        "mindroom.constants.runtime_matrix_homeserver",
        lambda _runtime_paths: "https://matrix.example.org",
    )
    monkeypatch.setattr(desktop_cli, "desktop_pair", pair)

    result = runner.invoke(desktop_app, ["setup", "--allow-agent", "computer", *_PAIRING_ARGS])

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
    headers_path = tmp_path / "matrix-http-headers.json"
    headers_path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    headers_path.chmod(0o600)
    bridge = AsyncMock()
    ensure_dependencies = _stub_run_command(monkeypatch, tmp_path)
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = runner.invoke(desktop_app, [*_RUN_ARGS, "--matrix-http-headers-file", str(headers_path)])

    assert result.exit_code == 0, result.output
    ensure_dependencies.assert_called_once_with(SimpleNamespace(storage_root=tmp_path))
    assert bridge.await_args.kwargs["http_headers"] == {"X-Access-Client": "test-secret"}


@pytest.mark.parametrize("shell_enabled", [False, True])
def test_desktop_run_refuses_a_saved_browser_without_apps_folders_or_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    shell_enabled: bool,
) -> None:
    """A browser alone cannot be controlled without a saved app, so the run says what to choose instead of starting."""
    save_native_config(
        native_config_path(tmp_path),
        replace(
            _run_config(roots=(), shell_enabled=shell_enabled),
            revision=0,
            browser=NativeBrowserConfig(enabled=True),
        ),
        expected_revision=0,
    )
    bridge = AsyncMock()
    _stub_run_command(monkeypatch, tmp_path)
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = runner.invoke(desktop_app, ["run"])

    assert result.exit_code == (0 if shell_enabled else 1), result.output
    assert bridge.await_count == int(shell_enabled)
    assert ("Choose and save apps, folders, or shell access" in result.output) is not shell_enabled


def test_desktop_run_restores_saved_cloudflare_access_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One-time login choice automatically applies to later bridge runs."""
    headers = MagicMock()
    access_headers = MagicMock(return_value=headers)
    bridge = AsyncMock()
    _stub_run_command(monkeypatch, tmp_path, cloudflare_access=True)
    monkeypatch.setattr("mindroom.desktop.session.desktop_http_headers", access_headers)
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = runner.invoke(desktop_app, _RUN_ARGS)

    assert result.exit_code == 0, result.output
    access_headers.assert_called_once_with("https://matrix.example.org", None, cloudflare_access=True)
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


@pytest.mark.parametrize(
    ("enabled", "executable"),
    [(False, "Brave"), (True, "missing-brave")],
    ids=["without_extension_mode", "missing_path"],
)
def test_browser_profile_paths_need_extension_mode_and_must_exist(
    tmp_path: Path,
    *,
    enabled: bool,
    executable: str,
) -> None:
    """Profile options are never silently ignored, and bad local paths fail before Matrix login and sync startup."""
    with pytest.raises(typer.Exit) as exc_info:
        desktop_cli._validate_browser_options(
            enabled=enabled,
            executable_path=tmp_path / executable,
            user_data_dir=None,
        )

    assert exc_info.value.exit_code == 2


@pytest.mark.usefixtures("cli_runtime")
def test_login_command_preserves_unexpected_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session persistence failures remain distinguishable from expected login errors."""
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


@pytest.mark.usefixtures("cli_runtime")
def test_run_command_preserves_unexpected_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected session I/O errors are not flattened into a friendly domain failure."""
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)

    def denied(_path: Path) -> None:
        message = "test session permission failure"
        raise PermissionError(message)

    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", denied)

    result = runner.invoke(desktop_app, _RUN_ARGS)

    assert isinstance(result.exception, PermissionError)


@pytest.mark.parametrize("app_id", ["chat.mindroom.menubar", "chat.mindroom.desktophelper"])
@pytest.mark.usefixtures("cli_runtime")
def test_run_refuses_mindroom_itself_as_a_one_run_app(
    monkeypatch: pytest.MonkeyPatch,
    app_id: str,
) -> None:
    """MindRoom's windows grant shell auto-approval and control, so `--allow-app` cannot name them for a run."""
    bridge = AsyncMock()
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = runner.invoke(desktop_app, [*_RUN_ARGS, "--allow-app", app_id])

    assert result.exit_code == 1
    assert f"control MindRoom itself ({app_id})" in " ".join(result.output.split())
    bridge.assert_not_awaited()


def _offline_run_deps(
    owner: FakeMatrixOwner,
    prepare_client: Callable[[nio.AsyncClient, PinnedMatrixDevice], Awaitable[None]] | None = None,
) -> desktop_cli._BridgeRunDeps:
    """Run the terminal bridge against a fake Matrix owner whose controller check passes unless told otherwise."""
    return desktop_cli._BridgeRunDeps(session=offline_session_deps(owner, prepare_client))


def _run_offline(
    tmp_path: Path,
    config: NativeDesktopConfig,
    deps: desktop_cli._BridgeRunDeps,
    *,
    allow_control: bool = False,
    shell_auto_approve_minutes: int | None = None,
) -> Coroutine[object, object, None]:
    """Return the terminal bridge run for the saved session with a 15-minute control lease."""
    return desktop_cli._run_bridge(
        runtime_paths=runtime_paths(tmp_path),
        session=SAVED_SESSION,
        config=config,
        allow_control=allow_control,
        lease_minutes=15,
        shell_auto_approve_minutes=shell_auto_approve_minutes,
        deps=deps,
    )


@pytest.mark.asyncio
async def test_run_without_shell_skips_shell_prompt_and_login_capture_and_fails_when_a_worker_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_selected_root: Path,
) -> None:
    """The control lease lasts the chosen minutes, and a worker returning on its own is never a clean exit."""
    policies: list[DesktopBridgePolicy] = []

    class Bridge(DesktopBridge):
        def __post_init__(self) -> None:
            super().__post_init__()
            policies.append(self.policy)

        async def run(self) -> None:
            return

    owner = FakeMatrixOwner()
    monkeypatch.setitem(sys.modules, "mindroom.desktop.shell_prompt", None)
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)

    with pytest.raises(DesktopWorkerStoppedError):
        await _run_offline(
            tmp_path,
            _run_config(roots=(empty_selected_root,)),
            _offline_run_deps(owner),
            allow_control=True,
        )

    # Stopping the bridge revokes control, so the lease is read as the run started it.
    [policy] = policies
    assert policy.allow_control is True
    assert policy.control_lease_expires_at_ms is not None
    assert 14 * 60 < policy.control_lease_expires_at_ms / 1000 - time.time() <= 15 * 60
    assert owner.closed.is_set()
    assert owner.client.to_device_callbacks == []


def test_desktop_run_reports_a_stopped_worker_as_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A run whose worker stopped on its own exits non-zero with the reason instead of looking like a clean stop."""
    _stub_run_command(monkeypatch, tmp_path)
    monkeypatch.setattr(desktop_cli, "_run_bridge", AsyncMock(side_effect=DesktopWorkerStoppedError()))

    result = runner.invoke(desktop_app, _RUN_ARGS)

    assert result.exit_code == 1
    assert "Desktop bridge failed: A desktop worker stopped unexpectedly." in " ".join(result.output.split())


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["transport_failure", "cancellation"])
async def test_cli_drains_native_work_before_releasing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_selected_root: Path,
    shutdown: str,
) -> None:
    """Neither transport failure nor cancellation may release ownership over live input."""
    started = asyncio.Event()
    shutdown_started = asyncio.Event()
    release_native = threading.Event()
    native_finished = threading.Event()
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

    class Owner(FakeMatrixOwner):
        async def close(self) -> None:
            shutdown_started.set()
            await super().close()

    source = FailingSource()
    owner = Owner(source=source)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", ActiveBridge)
    task = asyncio.create_task(
        _run_offline(tmp_path, _run_config(roots=(empty_selected_root,)), _offline_run_deps(owner)),
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        if shutdown == "cancellation":
            task.cancel()
        else:
            source.fail.set()
        await asyncio.wait_for(shutdown_started.wait(), 2)
        assert not owner.closed.is_set()
        assert not native_finished.is_set()
        assert not task.done()
        release_native.set()
        expected = asyncio.CancelledError if shutdown == "cancellation" else RuntimeError
        with pytest.raises(expected):
            await task
        assert native_finished.is_set()
        assert owner.closed.is_set()
    finally:
        release_native.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("apps", "expected_calls"),
    [((), ["open"]), (("com.example.Editor",), ["permissions", "open"])],
)
async def test_only_application_access_requests_gui_permissions_before_matrix_opens(
    tmp_path: Path,
    empty_selected_root: Path,
    apps: tuple[str, ...],
    expected_calls: list[str],
) -> None:
    """Applications need macOS GUI permissions before Matrix opens; folder and shell access never ask for them."""
    calls: list[str] = []

    async def unavailable_matrix(*_args: object) -> NoReturn:
        calls.append("open")
        msg = "Matrix unavailable"
        raise ConnectionError(msg)

    deps = desktop_cli._BridgeRunDeps(
        request_permissions=lambda: calls.append("permissions"),
        session=DesktopBridgeSessionDeps(open_client=unavailable_matrix),
    )
    with pytest.raises(ConnectionError, match="Matrix unavailable"):
        await _run_offline(tmp_path, _run_config(apps=apps, roots=(empty_selected_root,), shell_enabled=True), deps)

    assert calls == expected_calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_folder_and_shell_bridge_revokes_shell_access_on_stop(
    tmp_path: Path,
    empty_selected_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminal bridge serves folders and shell without a GUI provider, and stopping it ends the local grant."""
    prepared = asyncio.Event()

    async def prepare_client(*_args: object) -> None:
        prepared.set()

    owner = FakeMatrixOwner()
    task = asyncio.create_task(
        _run_offline(
            tmp_path,
            _run_config(roots=(empty_selected_root,), shell_enabled=True),
            _offline_run_deps(owner, prepare_client),
            shell_auto_approve_minutes=5,
        ),
    )
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        # The run registers exactly one admission callback: the built bridge's.
        [registration] = owner.client.to_device_callbacks
        bridge = registration.func.__self__
        assert isinstance(bridge, DesktopBridge)
        assert 290 < bridge.local_status()["shell"]["auto_approve_remaining_seconds"] <= 300
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    shell = bridge.shell
    assert shell is not None
    assert shell.status()["auto_approve_remaining_seconds"] == 0
    with pytest.raises(DesktopShellError, match="closed"):
        shell.grant(60)
    assert owner.client.to_device_callbacks == []
    output = " ".join(capsys.readouterr().out.split())
    assert "Applications: none" in output
    assert f"Read-only folders: {empty_selected_root}" in output
    assert "every locally allowed requester and agent" in output
    assert "for 5 minutes" in output
    assert "observe-only" not in output
    assert "emergency stop" not in output


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_terminal_approver_stops_reading_before_the_bridge_settles_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_selected_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stopping a shell run cancels terminal approval first, then drains the bridge and reports the revoked grant."""
    lifecycle: list[str] = []
    approving = asyncio.Event()

    class Bridge(DesktopBridge):
        async def stop(self) -> None:
            lifecycle.append("bridge_stop")
            await super().stop()

    async def approvals(*_args: object, **_kwargs: object) -> None:
        approving.set()
        try:
            await asyncio.Event().wait()
        finally:
            lifecycle.append("approvals_stopped")

    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)
    monkeypatch.setattr("mindroom.desktop.shell_prompt.serve_terminal_shell_approvals", approvals)
    config = _run_config(roots=(empty_selected_root,), shell_enabled=True)
    task = asyncio.create_task(_run_offline(tmp_path, config, _offline_run_deps(FakeMatrixOwner())))
    await asyncio.wait_for(approving.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert lifecycle == ["approvals_stopped", "bridge_stop"]
    assert "Shell access revoked; pending and running shell commands were stopped." in capsys.readouterr().out
