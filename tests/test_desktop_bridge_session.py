"""Tests for the Desktop bridge session shared by the native app helper and the terminal bridge."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import nio
import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent
from nio.durable import RecordKind, SyncBatch, SyncRecord

from mindroom.desktop import native_config
from mindroom.desktop.bridge import DesktopBridge
from mindroom.desktop.bridge_session import (
    DesktopBridgeSession,
    DesktopBridgeSessionDeps,
    DesktopWorkerStoppedError,
    first_worker_exit,
    start_desktop_bridge_session,
)
from mindroom.desktop.command_journal import DesktopCommandJournalError
from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeConfigError,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
)
from mindroom.desktop.protocol import DESKTOP_COMMAND_EVENT_TYPE
from mindroom.desktop.session import DesktopMatrixSession
from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellRequest
from mindroom.matrix.device_identity import PinnedMatrixDevice
from tests.desktop_helpers import IdleSource

if TYPE_CHECKING:
    from pathlib import Path

_SAVED_SESSION = DesktopMatrixSession("https://matrix.example.org", "@desktop:example.org", "DESKTOP", "token")


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


class _Owner:
    """Owned Matrix session stand-in that records closing."""

    def __init__(self, lifecycle: list[str], source: object | None = None) -> None:
        self.client = nio.AsyncClient(
            "https://matrix.example.org",
            config=nio.AsyncClientConfig(encryption_enabled=False),
        )
        self.source = source or IdleSource()
        self.lifecycle = lifecycle

    async def close(self) -> None:
        self.lifecycle.append("owner_close")
        await self.client.close()


def _offline(owner: _Owner, prepare: AsyncMock | None = None) -> DesktopBridgeSessionDeps:
    async def open_client(*_args: object) -> _Owner:
        owner.lifecycle.append("open")
        return owner

    return DesktopBridgeSessionDeps(open_client=open_client, prepare_client=prepare or AsyncMock())


async def _start(
    tmp_path: Path,
    config: NativeDesktopConfig,
    deps: DesktopBridgeSessionDeps,
    **grants: int,
) -> DesktopBridgeSession:
    return await start_desktop_bridge_session(
        config,
        _SAVED_SESSION,
        runtime_paths=_runtime_paths(tmp_path),
        http_headers=None,
        deps=deps,
        **grants,
    )


@pytest.fixture
def selected_root(tmp_path: Path) -> Path:
    """Create one selected folder."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    return root


@pytest.fixture
def login_environment(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the account login-shell capture with a fixed environment."""
    capture = AsyncMock(return_value={"PATH": os.environ["PATH"], "MINDROOM_CAPTURED": "from-login-shell"})
    monkeypatch.setattr("mindroom.desktop.login_environment.capture_login_environment", capture)
    return capture


class _RecordingBrowser:
    def __init__(self, lifecycle: list[str]) -> None:
        self.lifecycle = lifecycle

    async def close(self) -> None:
        self.lifecycle.append("browser_close")


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_failed_construction_closes_every_built_provider_and_the_matrix_owner(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge that cannot open its journal leaves no pinned folder, shell, browser, or Matrix owner behind."""
    lifecycle: list[str] = []

    class RecordingFilesystem(DesktopFilesystem):
        def close(self) -> None:
            lifecycle.append("filesystem_close")
            super().close()

    class RecordingShell(DesktopShell):
        async def close(self) -> None:
            lifecycle.append("shell_close")
            await super().close()

    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopFilesystem", RecordingFilesystem)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopShell", RecordingShell)
    monkeypatch.setattr(
        "mindroom.desktop.bridge_session.PlaywrightMCPBrowserProvider",
        lambda **_kwargs: _RecordingBrowser(lifecycle),
    )
    (tmp_path / "desktop_bridge" / "commands.sqlite3").mkdir(parents=True)
    owner = _Owner(lifecycle)

    with pytest.raises(DesktopCommandJournalError):
        await _start(tmp_path, _config(selected_root, browser=True), _offline(owner))

    assert lifecycle == ["open", "shell_close", "filesystem_close", "browser_close", "owner_close"]
    assert owner.client.to_device_callbacks == []


@pytest.mark.asyncio
async def test_saved_capabilities_define_the_bridge_without_a_gui_provider(
    tmp_path: Path,
    selected_root: Path,
    login_environment: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folder and shell runs build no GUI provider; a control lease is the only way to start with app input."""

    def forbidden_gui_provider(**_kwargs: object) -> None:
        pytest.fail("GUI provider constructed without application access")

    monkeypatch.setattr("mindroom.desktop.bridge_session.PyAutoGuiDesktopProvider", forbidden_gui_provider)

    session = await _start(tmp_path, _config(selected_root), _offline(_Owner([])), control_lease_seconds=600)

    bridge = session.bridge
    try:
        assert bridge.provider is None
        assert session.browser is None
        assert bridge.policy.allowed_file_roots == (selected_root,)
        assert bridge.policy.shell_enabled is True
        assert bridge.policy.allow_control is True
        assert bridge.policy.control_lease_expires_at_ms is not None
        assert 590 < bridge.policy.control_lease_expires_at_ms / 1000 - time.time() <= 600
        assert bridge.journal_path == tmp_path / "desktop_bridge" / "commands.sqlite3"
        assert bridge.legacy_journal_path == tmp_path / "desktop_bridge" / "command_journal.json"
        login_environment.assert_awaited_once()
        assert bridge.shell is not None
        bridge.shell.grant(60)
        request = DesktopShellRequest(
            "captured",
            "@person:example.org",
            "mind",
            'printf "$MINDROOM_CAPTURED"',
            str(tmp_path),
            round(time.time() * 1000) + 60_000,
        )
        result = await bridge.shell.execute(request)
        assert result.output.read() == b"from-login-shell"
        result.output.release()
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("folders", "shell"), [(True, False), (False, True), (True, True)])
async def test_without_posix_folder_and_shell_access_are_refused_before_building_them(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    folders: bool,
    shell: bool,
) -> None:
    """Folder and shell access need POSIX, so Windows names the fix instead of failing inside a provider."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Built folder or shell access for a run that must be refused")

    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopFilesystem", forbidden)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopShell", forbidden)
    monkeypatch.setattr(native_config, "_LOCAL_ACCESS_SUPPORTED", False)
    roots = (selected_root,) if folders else ()
    lifecycle: list[str] = []

    with pytest.raises(NativeConfigError, match="need macOS or Linux") as refused:
        await _start(tmp_path, _config(*roots, apps=("primary-screen",), shell=shell), _offline(_Owner(lifecycle)))

    assert "`mindroom desktop access --clear-folders --no-shell`" in str(refused.value)
    assert lifecycle == ["open", "owner_close"]


@pytest.mark.asyncio
async def test_without_posix_screenshot_only_app_observation_still_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshot-only app observation is the Windows target and loads no POSIX-only shell module."""
    provider = object()
    provider_options: list[dict[str, object]] = []

    def gui_provider(**kwargs: object) -> object:
        provider_options.append(kwargs)
        return provider

    monkeypatch.setattr("mindroom.desktop.bridge_session.PyAutoGuiDesktopProvider", gui_provider)
    monkeypatch.setattr(native_config, "_LOCAL_ACCESS_SUPPORTED", False)
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    # App observation reads the session's macOS application events, which this test does not need.
    monkeypatch.setattr("mindroom.desktop.bridge._run_macos_application_events", asyncio.Event().wait)

    session = await _start(tmp_path, _config(apps=("primary-screen",), shell=False), _offline(_Owner([])))

    bridge = session.bridge
    try:
        assert bridge.provider is provider
        assert provider_options[0]["allowed_app_ids"] == frozenset({"primary-screen"})
        assert (bridge.filesystem, bridge.shell, session.browser) == (None, None, None)
        assert (bridge.policy.allowed_file_roots, bridge.policy.shell_enabled) == ((), False)
        assert bridge.policy.allow_control is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_admits_before_acknowledging_and_closes_in_dependency_order(  # noqa: C901
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission is registered before the controller check, persists before ack, and close drains before releasing."""
    lifecycle: list[str] = []
    admitted = asyncio.Event()
    acked = asyncio.Event()
    ready = asyncio.Event()
    ready.set()
    event = AuthenticatedToDeviceEvent(
        source={"content": {}},
        sender="@cloud:example.org",
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice("@cloud:example.org", "CLOUD", "curve", "fingerprint"),
    )
    batch = SyncBatch(uuid4(), 1, (SyncRecord(RecordKind.TO_DEVICE, None, event.source),))

    class Source(IdleSource):
        async def wait_for_work(self) -> None:
            await ready.wait()

        async def next_batch(self) -> SyncBatch:
            ready.clear()
            return batch

        async def dispatch(self, _record: SyncRecord) -> None:
            await owner.client._on_to_device(event)

        async def ack(self, _batch: SyncBatch) -> None:
            assert admitted.is_set()
            lifecycle.append("ack")
            acked.set()

    class Bridge(DesktopBridge):
        async def on_to_device_event(self, event: object) -> None:  # noqa: ARG002
            lifecycle.append("admit")
            admitted.set()

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append("workers_cancelled")

        async def wait_for_capacity(self) -> None:
            msg = "unexpected capacity pressure"
            raise AssertionError(msg)

        async def stop(self) -> None:
            lifecycle.append("stop")
            await super().stop()

        def close(self) -> None:
            lifecycle.append("bridge_close")
            super().close()

    owner = _Owner(lifecycle, Source())

    async def prepare(prepared_client: object, controller: PinnedMatrixDevice) -> None:
        assert prepared_client is owner.client
        assert controller == _config().controller
        assert owner.client.to_device_callbacks
        lifecycle.append("prepare")

    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)

    session = await _start(
        tmp_path,
        _config(selected_root, shell=False),
        _offline(owner, AsyncMock(side_effect=prepare)),
    )
    await asyncio.wait_for(acked.wait(), 5)
    assert session.live
    await session.close()

    assert lifecycle == ["open", "prepare", "admit", "ack", "stop", "workers_cancelled", "bridge_close", "owner_close"]
    assert owner.client.to_device_callbacks == []
    assert not session.live


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_startup_failure_before_workers_releases_everything_without_stopping_the_bridge(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that never verified its controller delivers nothing, yet closes its shell, folders, browser, and owner."""
    lifecycle: list[str] = []
    bridges: list[DesktopBridge] = []

    class Bridge(DesktopBridge):
        def __post_init__(self) -> None:
            super().__post_init__()
            bridges.append(self)

        async def stop(self) -> None:
            pytest.fail("A bridge whose controller was never verified must not deliver replies")

    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)
    monkeypatch.setattr(
        "mindroom.desktop.bridge_session.PlaywrightMCPBrowserProvider",
        lambda **_kwargs: _RecordingBrowser(lifecycle),
    )
    owner = _Owner(lifecycle)
    prepare = AsyncMock(side_effect=RuntimeError("controller device changed"))

    with pytest.raises(RuntimeError, match="controller device changed"):
        await _start(tmp_path, _config(selected_root, browser=True), _offline(owner, prepare))

    [bridge] = bridges
    assert lifecycle == ["open", "browser_close", "owner_close"]
    assert owner.client.to_device_callbacks == []
    assert bridge.shell is not None
    with pytest.raises(DesktopShellError, match="closed"):
        bridge.shell.grant(60)
    assert bridge.filesystem is not None
    with pytest.raises(DesktopFilesystemError, match="closed"):
        bridge.filesystem.list_folders()


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_concurrent_closes_share_one_teardown_that_a_cancelled_caller_cannot_cut_short(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that cancels a closing supervisor still waits for that same close to finish."""
    lifecycle: list[str] = []
    stopping = asyncio.Event()
    release_stop = asyncio.Event()

    class Bridge(DesktopBridge):
        async def stop(self) -> None:
            lifecycle.append("stop")
            stopping.set()
            await release_stop.wait()
            await super().stop()

    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)
    session = await _start(tmp_path, _config(selected_root), _offline(_Owner(lifecycle)))
    first = asyncio.create_task(session.close())
    await stopping.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    second = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not second.done()
    release_stop.set()
    await second
    await session.close()

    assert lifecycle == ["open", "stop", "owner_close"]


@pytest.mark.asyncio
async def test_close_fences_admission_and_ends_control_before_it_yields(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing revokes the control lease and stops the bridge at once, before any release step can yield."""
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    session = await _start(
        tmp_path,
        _config(selected_root, shell=False),
        _offline(_Owner([])),
        control_lease_seconds=600,
    )
    assert session.bridge.policy.allow_control is True

    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0)

    assert session.bridge.policy.allow_control is False
    assert session.bridge.local_status()["gui_mode"] == "stopped"
    await closing


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_a_failing_release_still_releases_the_rest_and_is_the_close_outcome(
    tmp_path: Path,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One release that raises never keeps the Matrix owner open, and every caller learns of the failure."""
    lifecycle: list[str] = []

    class FailingBrowser(_RecordingBrowser):
        async def close(self) -> None:
            await super().close()
            msg = "browser would not close"
            raise RuntimeError(msg)

    monkeypatch.setattr(
        "mindroom.desktop.bridge_session.PlaywrightMCPBrowserProvider",
        lambda **_kwargs: FailingBrowser(lifecycle),
    )
    session = await _start(tmp_path, _config(selected_root, browser=True), _offline(_Owner(lifecycle)))

    with pytest.raises(RuntimeError, match="browser would not close"):
        await session.close()
    with pytest.raises(RuntimeError, match="browser would not close"):
        await session.close()

    assert lifecycle == ["open", "browser_close", "owner_close"]
    assert not session.live


@pytest.mark.asyncio
async def test_first_worker_exit_reports_the_failure_and_treats_a_normal_return_as_unexpected() -> None:
    """A lifetime worker never ends on its own, so a normal return is a failure too; cancellation stays cancellation."""

    async def fail() -> None:
        msg = "transport failed"
        raise RuntimeError(msg)

    async def finish() -> None:
        return

    idle = asyncio.create_task(asyncio.Event().wait())
    failure = await first_worker_exit((asyncio.create_task(fail()), idle))
    assert str(failure) == "transport failed"
    assert isinstance(await first_worker_exit((asyncio.create_task(finish()), idle)), DesktopWorkerStoppedError)
    cancelled = asyncio.create_task(asyncio.Event().wait())
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_worker_exit((cancelled, idle))
    idle.cancel()


@pytest.mark.asyncio
async def test_default_matrix_preparation_checks_the_pinned_controller_before_publishing_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run publishes this device's encryption keys only after checking the pinned controller."""
    client = object()
    controller = PinnedMatrixDevice("@cloud:example.org", "CLOUD", "fingerprint")
    calls: list[tuple[object, ...]] = []

    async def resolve(*args: object) -> None:
        calls.append(("resolve", *args))

    async def prepare(*args: object) -> None:
        calls.append(("prepare", *args))

    monkeypatch.setattr("mindroom.desktop.bridge_session.resolve_pinned_device", resolve)
    monkeypatch.setattr("mindroom.desktop.bridge_session.prepare_desktop_client", prepare)

    await DesktopBridgeSessionDeps().prepare_client(client, controller)

    assert calls == [("resolve", client, controller), ("prepare", client)]
