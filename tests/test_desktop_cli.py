"""Tests for the local desktop bridge CLI lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
import typer
from typer.testing import CliRunner

import mindroom.cli.desktop as desktop_cli
from mindroom.cli.desktop import desktop_app
from mindroom.desktop.session import DesktopMatrixSession, DesktopSessionError
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


def test_desktop_login_accepts_explicit_homeserver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh local machine can target cloud Matrix without hidden environment setup."""
    runtime_paths = SimpleNamespace(storage_root=tmp_path)
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
        ],
    )

    assert result.exit_code == 0, result.output
    assert login.await_args.kwargs["homeserver"] == "https://matrix.example.org"


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
async def test_bridge_registers_command_callback_before_initial_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queued encrypted commands are dispatched instead of consumed during startup."""
    client = _FakeBridgeClient()
    bridge = SimpleNamespace(on_to_device_event=AsyncMock())
    lifecycle: list[str] = []

    async def open_client(*_args: object, **_kwargs: object) -> _FakeBridgeClient:
        lifecycle.append("open")
        return client

    async def prepare_client(preparing_client: _FakeBridgeClient) -> None:
        lifecycle.append("prepare")
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
        lifecycle.append("resolve")

    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.session.prepare_desktop_client", prepare_client)
    monkeypatch.setattr("mindroom.matrix.olm_to_device.resolve_pinned_device", resolve_device)
    monkeypatch.setattr("mindroom.desktop.provider.PyAutoGuiDesktopProvider", lambda **_kwargs: object())
    monkeypatch.setattr("mindroom.desktop.bridge.DesktopBridge", lambda **_kwargs: bridge)

    await desktop_cli._run_bridge(
        runtime_paths=SimpleNamespace(),
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

    assert lifecycle == ["open", "prepare", "resolve"]
    bridge.on_to_device_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_sync_error_stops_bridge_with_clear_failure() -> None:
    """A revoked desktop token exits instead of spinning under an online banner."""
    client = _FakeBridgeClient()
    client.sync_error = nio.SyncError("Access token revoked", status_code="M_UNKNOWN_TOKEN")

    with pytest.raises(DesktopSessionError, match="permanent authentication failure"):
        await desktop_cli._sync_desktop_client(client)

    assert client.stopped
