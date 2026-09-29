"""Tests for the bridge builder shared by the native app helper and the terminal bridge."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.bridge_components import build_desktop_bridge
from mindroom.desktop.command_journal import DesktopCommandJournalError
from mindroom.desktop.filesystem import DesktopFilesystem
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
)
from mindroom.desktop.shell import DesktopShell, DesktopShellRequest
from mindroom.matrix.device_identity import PinnedMatrixDevice

if TYPE_CHECKING:
    from pathlib import Path


def _config(
    *roots: Path,
    apps: tuple[str, ...] = (),
    browser: bool = False,
    shell: bool = True,
) -> NativeDesktopConfig:
    return NativeDesktopConfig(
        revision=3,
        enabled=True,
        controller=PinnedMatrixDevice("@controller:example.org", "CLOUD", "fingerprint"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("mind",),
        allowed_app_ids=apps,
        capture=NativeCaptureConfig(),
        browser=NativeBrowserConfig(enabled=browser),
        files=NativeFilesConfig(roots),
        shell=NativeShellConfig(enabled=shell),
    )


def _runtime_paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None)


@pytest.fixture
def selected_root(tmp_path: Path) -> Path:
    """Create one selected folder."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    return root


@pytest.fixture
def login_environment(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the account login-shell capture with a fixed environment."""
    capture = AsyncMock(return_value={"PATH": os.defpath, "MINDROOM_CAPTURED": "from-login-shell"})
    monkeypatch.setattr("mindroom.desktop.login_environment.capture_login_environment", capture)
    return capture


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_failed_construction_closes_every_built_provider_once(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge that cannot open its journal leaves no pinned folder, shell, or browser behind."""
    closed: list[str] = []

    class RecordingFilesystem(DesktopFilesystem):
        def close(self) -> None:
            closed.append("filesystem")
            super().close()

    class RecordingShell(DesktopShell):
        async def close(self) -> None:
            closed.append("shell")
            await super().close()

    class RecordingBrowser:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def close(self) -> None:
            closed.append("browser")

    monkeypatch.setattr("mindroom.desktop.bridge_components.DesktopFilesystem", RecordingFilesystem)
    monkeypatch.setattr("mindroom.desktop.bridge_components.DesktopShell", RecordingShell)
    monkeypatch.setattr("mindroom.desktop.bridge_components.PlaywrightMCPBrowserProvider", RecordingBrowser)
    (tmp_path / "desktop_bridge" / "commands.sqlite3").mkdir(parents=True)

    with pytest.raises(DesktopCommandJournalError):
        await build_desktop_bridge(
            _config(selected_root, browser=True),
            client=object(),
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert sorted(closed) == ["browser", "filesystem", "shell"]


@pytest.mark.asyncio
async def test_saved_capabilities_define_the_bridge_without_a_gui_provider(
    tmp_path: Path,
    selected_root: Path,
    login_environment: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folder and shell runs build no GUI provider; a lease expiry is the only way to start with app input."""

    def forbidden_gui_provider(**_kwargs: object) -> None:
        pytest.fail("GUI provider constructed without application access")

    monkeypatch.setattr("mindroom.desktop.bridge_components.PyAutoGuiDesktopProvider", forbidden_gui_provider)
    lease = round((time.time() + 600) * 1000)

    components = await build_desktop_bridge(
        _config(selected_root),
        client=object(),
        runtime_paths=_runtime_paths(tmp_path),
        control_lease_expires_at_ms=lease,
    )

    bridge = components.bridge
    try:
        assert bridge.provider is None
        assert components.browser is None
        assert bridge.filesystem is components.filesystem
        assert bridge.shell is components.shell
        assert bridge.policy.allowed_file_roots == (selected_root,)
        assert bridge.policy.shell_enabled is True
        assert (bridge.policy.allow_control, bridge.policy.control_lease_expires_at_ms) == (True, lease)
        login_environment.assert_awaited_once()
        assert components.shell is not None
        components.shell.grant(60)
        request = DesktopShellRequest(
            "captured",
            "@person:example.org",
            "mind",
            'printf "$MINDROOM_CAPTURED"',
            str(tmp_path),
            round(time.time() * 1000) + 60_000,
        )
        result = await components.shell.execute(request)
        assert result.output.read() == b"from-login-shell"
        result.output.release()
    finally:
        await bridge.stop()
        bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("folders", "shell"), [(True, False), (False, True), (True, True)])
async def test_windows_refuses_folder_and_shell_access_before_building_them(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    folders: bool,
    shell: bool,
) -> None:
    """Folder and shell access need POSIX, so Windows names the fix instead of failing inside a provider."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Windows built folder or shell access for a run it must refuse")

    monkeypatch.setattr("mindroom.desktop.bridge_components.DesktopFilesystem", forbidden)
    monkeypatch.setattr("mindroom.desktop.bridge_components.DesktopShell", forbidden)
    roots = (selected_root,) if folders else ()
    config = _config(*roots, apps=("primary-screen",), shell=shell)

    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        with pytest.raises(ValueError, match="need macOS or Linux") as refused:
            await build_desktop_bridge(config, client=object(), runtime_paths=_runtime_paths(tmp_path))

    assert "`mindroom desktop access --clear-folders --no-shell`" in str(refused.value)


@pytest.mark.asyncio
async def test_windows_builds_screenshot_only_app_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshot-only app observation is the Windows target and loads no POSIX-only shell module."""
    provider = object()
    provider_options: list[dict[str, object]] = []

    def gui_provider(**kwargs: object) -> object:
        provider_options.append(kwargs)
        return provider

    monkeypatch.setattr("mindroom.desktop.bridge_components.PyAutoGuiDesktopProvider", gui_provider)
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)

    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        components = await build_desktop_bridge(
            _config(apps=("primary-screen",), shell=False),
            client=object(),
            runtime_paths=_runtime_paths(tmp_path),
        )

    bridge = components.bridge
    try:
        assert bridge.provider is provider
        assert provider_options[0]["allowed_app_ids"] == frozenset({"primary-screen"})
        assert (components.filesystem, components.shell, components.browser) == (None, None, None)
        assert (bridge.policy.allowed_file_roots, bridge.policy.shell_enabled) == ((), False)
        assert bridge.policy.allow_control is False
    finally:
        await bridge.stop()
        bridge.close()
