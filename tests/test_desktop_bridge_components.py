"""Tests for the bridge builder shared by the native app helper and the terminal bridge."""

from __future__ import annotations

import os
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


def _config(root: Path, *, apps: tuple[str, ...] = (), browser: bool = False) -> NativeDesktopConfig:
    return NativeDesktopConfig(
        revision=3,
        enabled=True,
        controller=PinnedMatrixDevice("@controller:example.org", "CLOUD", "fingerprint"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("mind",),
        allowed_app_ids=apps,
        capture=NativeCaptureConfig(),
        browser=NativeBrowserConfig(enabled=browser),
        files=NativeFilesConfig((root,)),
        shell=NativeShellConfig(enabled=True),
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
    monkeypatch.setattr("mindroom.desktop.bridge_components.capture_login_environment", capture)
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
