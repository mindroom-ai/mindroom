"""The terminal and native app share one saved Desktop connection and allowlist."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from mindroom.cli import desktop as desktop_cli
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeDesktopConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)
from mindroom.desktop.native_host import NativeDesktopHost
from mindroom.desktop.session import DesktopMatrixSession, load_desktop_session, save_desktop_session
from mindroom.matrix.device_identity import PinnedMatrixDevice

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def shared_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Keep real saved state and replace only network/platform effects."""
    paths = SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None)
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://example.org", "@person:example.org", "LOCAL", "private-token"),
    )
    monkeypatch.setattr(desktop_cli, "_activate_desktop_runtime", lambda *_args, **_kwargs: paths)
    monkeypatch.setattr(desktop_cli, "_ensure_desktop_dependencies", lambda *_: None)
    monkeypatch.setattr("mindroom.logging_config.setup_logging", lambda **_: None)
    return paths


def _config() -> NativeDesktopConfig:
    return NativeDesktopConfig(
        revision=0,
        enabled=True,
        controller=PinnedMatrixDevice("@controller:example.org", "CLOUD", "fingerprint"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("mind",),
        allowed_app_ids=("com.apple.TextEdit",),
        capture=NativeCaptureConfig(max_screenshot_width=1200, jpeg_quality=75),
        browser=NativeBrowserConfig(),
    )


def _setup_args() -> list[str]:
    return [
        "setup",
        "--homeserver",
        "https://example.org",
        "--code",
        "short-lived-code",
        "--controller-user-id",
        "@controller:example.org",
        "--controller-device-id",
        "CLOUD",
        "--controller-ed25519",
        "fingerprint",
        "--allow-agent",
        "mind",
    ]


def test_cli_setup_is_visible_to_an_already_open_app(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful terminal claim publishes non-secret settings without restarting the helper."""
    host = NativeDesktopHost(shared_setup, helper_version="test")
    monkeypatch.setattr(desktop_cli, "_pair_desktop", AsyncMock(return_value="verification"))
    assert host.status()["config"]["state"] == "missing"

    result = CliRunner().invoke(desktop_cli.desktop_app, [*_setup_args(), "--allow-app", "com.apple.TextEdit"])

    assert result.exit_code == 0, result.output
    saved = load_native_config(native_config_path(shared_setup.storage_root))
    assert saved.allowed_agent_names == ("mind",)
    assert saved.allowed_requester_ids == ("@person:example.org",)
    assert saved.allowed_app_ids == ("com.apple.TextEdit",)
    assert host.status()["config"]["controller_device_id"] == "CLOUD"
    assert "short-lived-code" not in repr(saved.to_payload())
    assert "private-token" not in repr(host.status())


def test_repairing_preserves_saved_app_choices(shared_setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pairing the same controller again keeps the app's explicit local choices."""
    path = native_config_path(shared_setup.storage_root)
    previous = save_native_config(path, _config(), expected_revision=0)
    monkeypatch.setattr(desktop_cli, "_pair_desktop", AsyncMock(return_value="verification"))

    result = CliRunner().invoke(desktop_cli.desktop_app, _setup_args())

    assert result.exit_code == 0, result.output
    saved = load_native_config(path)
    assert saved.allowed_app_ids == previous.allowed_app_ids
    assert saved.capture == previous.capture
    assert saved.browser == previous.browser


@pytest.mark.parametrize("existing", [False, True])
def test_setup_from_an_older_server_resolves_agent_explicitly(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: bool,
) -> None:
    """Older deployed commands omit agent flags; ask rather than guess from a saved controller."""
    path = native_config_path(shared_setup.storage_root)
    if existing:
        save_native_config(path, _config(), expected_revision=0)
    monkeypatch.setattr(desktop_cli, "_pair_desktop", AsyncMock(return_value="verification"))
    args = _setup_args()[:-2]

    result = CliRunner().invoke(desktop_cli.desktop_app, args, input="mind\n")

    assert result.exit_code == 0, result.output
    assert load_native_config(path).allowed_agent_names == ("mind",)
    assert "Agent name from the setup message" in result.output


def test_failed_pair_does_not_replace_saved_setup(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired pairing code cannot replace existing app access."""
    path = native_config_path(shared_setup.storage_root)
    previous = save_native_config(path, _config(), expected_revision=0)
    monkeypatch.setattr(desktop_cli, "_pair_desktop", AsyncMock(side_effect=ValueError("expired code")))

    result = CliRunner().invoke(desktop_cli.desktop_app, [*_setup_args(), "--allow-app", "com.example.New"])

    assert result.exit_code == 1
    assert "expired code" in result.output
    assert load_native_config(path) == previous


@pytest.mark.parametrize("succeeds", [False, True])
def test_setup_remembers_cloudflare_access_after_a_successful_claim(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeeds: bool,
) -> None:
    """An older login upgraded through terminal setup also authenticates later app starts."""
    pair = AsyncMock(return_value="verification", side_effect=None if succeeds else ValueError("expired code"))
    monkeypatch.setattr(desktop_cli, "_pair_desktop", pair)
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.cloudflare_access_headers", lambda *_: {})

    result = CliRunner().invoke(desktop_cli.desktop_app, [*_setup_args(), "--cloudflare-access"])

    assert result.exit_code == (0 if succeeds else 1), result.output
    session = load_desktop_session(shared_setup.storage_root / "desktop_bridge" / "matrix_session.json")
    assert session.cloudflare_access is succeeds


def test_setup_does_not_overwrite_a_login_replaced_during_pairing(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata persistence cannot resurrect the old device after another interface logs in."""
    path = shared_setup.storage_root / "desktop_bridge" / "matrix_session.json"
    replacement = DesktopMatrixSession("https://example.org", "@person:example.org", "NEW", "new-token")

    async def replace_login(**_: object) -> str:
        save_desktop_session(path, replacement)
        return "verification"

    monkeypatch.setattr(desktop_cli, "_pair_desktop", replace_login)
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.cloudflare_access_headers", lambda *_: {})

    result = CliRunner().invoke(desktop_cli.desktop_app, [*_setup_args(), "--cloudflare-access"])

    assert result.exit_code == 1
    assert load_desktop_session(path) == replacement
    assert not native_config_path(shared_setup.storage_root).exists()


def test_cli_run_uses_app_settings_and_remains_observe_only(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app-configured bridge runs with no repeated controller or application flags."""
    path = native_config_path(shared_setup.storage_root)
    saved = save_native_config(path, _config(), expected_revision=0)
    bridge = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = CliRunner().invoke(desktop_cli.desktop_app, ["run"])

    assert result.exit_code == 0, result.output
    args = bridge.await_args.kwargs
    assert args["controller_device_id"] == "CLOUD"
    assert args["allow_app"] == frozenset({"com.apple.TextEdit"})
    assert args["allow_agent"] == frozenset({"mind"})
    assert args["max_screenshot_width"] == 1200
    assert args["jpeg_quality"] == 75
    assert args["allow_control"] is False
    assert load_native_config(path) == saved


def test_cli_run_does_not_mix_different_controller_with_saved_authority(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial pin override never inherits a different controller's local authorization."""
    save_native_config(native_config_path(shared_setup.storage_root), _config(), expected_revision=0)
    bridge = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = CliRunner().invoke(desktop_cli.desktop_app, ["run", "--controller-device-id", "OTHER"])

    assert result.exit_code != 0
    assert "controller" in result.output.lower()
    bridge.assert_not_called()


@pytest.mark.parametrize("state", ["missing", "disabled", "malformed"])
def test_cli_run_reports_unusable_saved_setup(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Expected saved-configuration failures stay in the CLI error boundary."""
    path = native_config_path(shared_setup.storage_root)
    if state != "missing":
        save_native_config(path, replace(_config(), enabled=False), expected_revision=0)
    if state == "malformed":
        path.write_text("not JSON")
    bridge = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = CliRunner().invoke(desktop_cli.desktop_app, ["run"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Desktop bridge failed:" in result.output
    bridge.assert_not_called()


def test_setup_reports_a_concurrent_app_save_without_overwriting_it(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app edit during pairing remains saved and gives the terminal a readable conflict."""
    path = native_config_path(shared_setup.storage_root)
    previous = save_native_config(path, _config(), expected_revision=0)

    async def save_app_choices(**_: object) -> str:
        save_native_config(path, replace(previous, allowed_app_ids=()), expected_revision=previous.revision)
        return "verification"

    monkeypatch.setattr(desktop_cli, "_pair_desktop", save_app_choices)

    result = CliRunner().invoke(desktop_cli.desktop_app, _setup_args())

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Desktop setup failed:" in result.output
    assert load_native_config(path).allowed_app_ids == ()


def test_cli_run_keeps_relative_browser_path_support(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit CLI paths resolve before entering the app's absolute-path configuration schema."""
    root = shared_setup.storage_root
    (root / "browser").touch()
    monkeypatch.chdir(root)
    save_native_config(native_config_path(root), _config(), expected_revision=0)
    bridge = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)

    result = CliRunner().invoke(
        desktop_cli.desktop_app,
        ["run", "--browser-extension", "--browser-executable", "browser", "--browser-user-data-dir", "."],
    )

    assert result.exit_code == 0, result.output
    assert bridge.await_args.kwargs["browser_executable"] == (root / "browser").resolve()
    assert bridge.await_args.kwargs["browser_user_data_dir"] == root.resolve()


def test_idle_host_refreshes_external_changes_and_removal(shared_setup: SimpleNamespace) -> None:
    """Polling follows the shared file instead of retaining a stale allowlist."""
    path = native_config_path(shared_setup.storage_root)
    first = save_native_config(path, _config(), expected_revision=0)
    host = NativeDesktopHost(shared_setup, helper_version="test")
    save_native_config(path, replace(first, allowed_app_ids=("com.example.New",)), expected_revision=first.revision)
    assert host.status()["config"]["allowed_app_ids"] == ["com.example.New"]
    path.unlink()
    assert host.status()["config"]["state"] == "missing"


@pytest.mark.parametrize("command", ["setup", "run"])
def test_invalid_controller_has_a_cli_error_before_network_activity(
    shared_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Malformed pasted identities fail clearly instead of escaping as a traceback."""
    pair = AsyncMock()
    bridge = AsyncMock()
    monkeypatch.setattr(desktop_cli, "_pair_desktop", pair)
    monkeypatch.setattr(desktop_cli, "_run_bridge", bridge)
    args = _setup_args()
    args[args.index("--controller-user-id") + 1] = "invalid-controller"
    if command == "run":
        args = ["run", *args[5:]]
    result = CliRunner().invoke(desktop_cli.desktop_app, args)
    assert result.exit_code == 1
    assert "@user:server" in result.output
    assert not native_config_path(shared_setup.storage_root).exists()
    pair.assert_not_called()
    bridge.assert_not_called()
