"""Tests for the Desktop bridge session shared by the native app helper and the terminal bridge."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent
from nio.durable import RecordKind, SyncBatch, SyncRecord

from mindroom.desktop import native_config
from mindroom.desktop.accessibility import AccessibilityState, DesktopRect
from mindroom.desktop.bridge import DesktopBridge
from mindroom.desktop.bridge_session import (
    DesktopBridgeSession,
    DesktopBridgeSessionDeps,
    DesktopWorkerStoppedError,
    first_worker_exit,
    start_desktop_bridge_session,
)
from mindroom.desktop.command_journal import (
    DesktopCommandJournal,
    DesktopCommandJournalError,
    controller_journal_key,
    desktop_journal_path,
)
from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeConfigError,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
)
from mindroom.desktop.protocol import DESKTOP_COMMAND_EVENT_TYPE, DesktopCommand
from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellRequest
from mindroom.matrix.device_identity import PinnedMatrixDevice
from tests.desktop_helpers import (
    SAVED_SESSION,
    FakeMatrixOwner,
    FakeProvider,
    IdleSource,
    ManualBridge,
    offline_session_deps,
    runtime_paths,
)
from tests.desktop_helpers import _fake_quartz as fake_quartz
from tests.desktop_helpers import _provider as real_provider
from tests.desktop_helpers import _window as window
from tests.desktop_helpers import empty_selected_root as empty_selected_root  # noqa: PLC0414
from tests.desktop_helpers import login_environment as login_environment  # noqa: PLC0414

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_CONTROLLER = PinnedMatrixDevice("@controller:example.org", "CLOUD", "fingerprint")


def _config(
    *roots: Path,
    apps: tuple[str, ...] = (),
    browser: bool = False,
    shell: bool = True,
) -> NativeDesktopConfig:
    return NativeDesktopConfig(
        revision=3,
        enabled=True,
        controller=_CONTROLLER,
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("mind",),
        allowed_app_ids=apps,
        capture=NativeCaptureConfig(),
        browser=NativeBrowserConfig(enabled=browser),
        files=NativeFilesConfig(roots),
        shell=NativeShellConfig(enabled=shell),
    )


async def _start(
    tmp_path: Path,
    config: NativeDesktopConfig,
    deps: DesktopBridgeSessionDeps,
    **grants: int,
) -> DesktopBridgeSession:
    return await start_desktop_bridge_session(
        config,
        SAVED_SESSION,
        runtime_paths=runtime_paths(tmp_path),
        http_headers=None,
        deps=deps,
        **grants,
    )


class _RecordingBrowser:
    def __init__(self, lifecycle: list[str]) -> None:
        self.lifecycle = lifecycle

    async def close(self) -> None:
        self.lifecycle.append("browser_close")


def _patch_browser(monkeypatch: pytest.MonkeyPatch, browser: object) -> None:
    monkeypatch.setattr("mindroom.desktop.bridge_session.PlaywrightMCPBrowserProvider", lambda **_kwargs: browser)


@pytest.mark.asyncio
@pytest.mark.usefixtures("login_environment")
async def test_failed_construction_closes_every_built_provider_and_the_matrix_owner(
    tmp_path: Path,
    empty_selected_root: Path,
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
    _patch_browser(monkeypatch, _RecordingBrowser(lifecycle))
    (tmp_path / "desktop_bridge" / "commands.sqlite3").mkdir(parents=True)
    owner = FakeMatrixOwner(lifecycle)

    with pytest.raises(DesktopCommandJournalError):
        await _start(tmp_path, _config(empty_selected_root, browser=True), offline_session_deps(owner))

    assert lifecycle == ["open", "shell_close", "filesystem_close", "browser_close", "owner_close"]
    assert owner.client.to_device_callbacks == []


@pytest.mark.asyncio
async def test_saved_capabilities_define_the_bridge_without_a_gui_provider(
    tmp_path: Path,
    empty_selected_root: Path,
    login_environment: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folder and shell runs build no GUI provider; a control lease is the only way to start with app input."""

    def forbidden_gui_provider(**_kwargs: object) -> None:
        pytest.fail("GUI provider constructed without application access")

    monkeypatch.setattr("mindroom.desktop.bridge_session.PyAutoGuiDesktopProvider", forbidden_gui_provider)

    session = await _start(
        tmp_path,
        _config(empty_selected_root),
        offline_session_deps(FakeMatrixOwner()),
        control_lease_seconds=600,
    )

    bridge = session.bridge
    try:
        assert bridge.provider is None
        assert session.browser is None
        assert bridge.policy.allowed_file_roots == (empty_selected_root,)
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
    empty_selected_root: Path,
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
    roots = (empty_selected_root,) if folders else ()
    lifecycle: list[str] = []

    with pytest.raises(NativeConfigError, match="need macOS or Linux") as refused:
        await _start(
            tmp_path,
            _config(*roots, apps=("primary-screen",), shell=shell),
            offline_session_deps(FakeMatrixOwner(lifecycle)),
        )

    assert "`mindroom desktop access --clear-folders --no-shell`" in str(refused.value)
    assert lifecycle == ["open", "owner_close"]


@pytest.mark.asyncio
async def test_without_posix_screenshot_only_app_observation_still_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshot-only app observation is the Windows target and loads no POSIX-only shell module."""
    provider = FakeProvider()
    gui_provider = Mock(return_value=provider)
    monkeypatch.setattr("mindroom.desktop.bridge_session.PyAutoGuiDesktopProvider", gui_provider)
    monkeypatch.setattr(native_config, "_LOCAL_ACCESS_SUPPORTED", False)
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    # App observation reads the session's macOS application events, which this test does not need.
    monkeypatch.setattr("mindroom.desktop.bridge._run_macos_application_events", asyncio.Event().wait)

    session = await _start(
        tmp_path,
        _config(apps=("primary-screen",), shell=False),
        offline_session_deps(FakeMatrixOwner()),
    )

    bridge = session.bridge
    try:
        assert bridge.provider is provider
        assert gui_provider.call_args_list[0].kwargs["allowed_app_ids"] == frozenset({"primary-screen"})
        assert (bridge.filesystem, bridge.shell, session.browser) == (None, None, None)
        assert (bridge.policy.allowed_file_roots, bridge.policy.shell_enabled) == ((), False)
        assert bridge.policy.allow_control is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_admits_before_acknowledging_and_closes_in_dependency_order(  # noqa: C901
    tmp_path: Path,
    empty_selected_root: Path,
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

    class Bridge(ManualBridge):
        async def on_to_device_event(self, event: object) -> None:  # noqa: ARG002
            lifecycle.append("admit")
            admitted.set()

        async def run(self) -> None:
            try:
                await super().run()
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

    owner = FakeMatrixOwner(lifecycle, Source())

    async def prepare(prepared_client: object, controller: PinnedMatrixDevice) -> None:
        assert prepared_client is owner.client
        assert controller == _CONTROLLER
        assert owner.client.to_device_callbacks
        lifecycle.append("prepare")

    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    monkeypatch.setattr("mindroom.desktop.bridge_session.DesktopBridge", Bridge)

    session = await _start(
        tmp_path,
        _config(empty_selected_root, shell=False),
        offline_session_deps(owner, prepare),
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
    empty_selected_root: Path,
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
    _patch_browser(monkeypatch, _RecordingBrowser(lifecycle))
    owner = FakeMatrixOwner(lifecycle)
    prepare = AsyncMock(side_effect=RuntimeError("controller device changed"))

    with pytest.raises(RuntimeError, match="controller device changed"):
        await _start(tmp_path, _config(empty_selected_root, browser=True), offline_session_deps(owner, prepare))

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
    empty_selected_root: Path,
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
    session = await _start(tmp_path, _config(empty_selected_root), offline_session_deps(FakeMatrixOwner(lifecycle)))
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
    empty_selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing revokes the control lease and stops the bridge at once, before any release step can yield."""
    monkeypatch.setitem(sys.modules, "mindroom.desktop.login_environment", None)
    session = await _start(
        tmp_path,
        _config(empty_selected_root, shell=False),
        offline_session_deps(FakeMatrixOwner()),
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
    empty_selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One release that raises never keeps the Matrix owner open, and every caller learns of the failure."""
    lifecycle: list[str] = []

    class FailingBrowser(_RecordingBrowser):
        async def close(self) -> None:
            await super().close()
            msg = "browser would not close"
            raise RuntimeError(msg)

    _patch_browser(monkeypatch, FailingBrowser(lifecycle))
    session = await _start(
        tmp_path,
        _config(empty_selected_root, browser=True),
        offline_session_deps(FakeMatrixOwner(lifecycle)),
    )

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


def _record_late_journal_writes(
    journal: DesktopCommandJournal,
    monkeypatch: pytest.MonkeyPatch,
    on_close: Callable[[], None],
) -> list[str]:
    """Return the journal writes attempted after `journal` closed; `on_close` runs right after it closes."""
    late_writes: list[str] = []
    closed = threading.Event()
    for name in ("remember_started", "remember_response", "queue_response", "mark_delivered"):
        write = getattr(journal, name)

        def record(*args: object, _name: str = name, _write: Callable[..., None] = write) -> None:
            if closed.is_set():
                late_writes.append(_name)
            _write(*args)

        monkeypatch.setattr(journal, name, record)
    close = journal.close

    def close_and_notify() -> None:
        close()
        closed.set()
        on_close()

    monkeypatch.setattr(journal, "close", close_and_notify)
    return late_writes


def _drag_event(session: DesktopBridgeSession) -> AuthenticatedToDeviceEvent:
    """Build an authenticated drag command for the session's app, with its observed state already retained."""
    now_ms = round(time.time() * 1000)
    drag = DesktopCommand(
        request_id="drag-1",
        session_id="session-1",
        sequence=1,
        issued_at_ms=now_ms,
        expires_at_ms=now_ms + 60_000,
        action="drag",
        requester_id="@person:example.org",
        agent_name="mind",
        parameters={
            "app": "com.example.Editor",
            "state_id": "state-1",
            "start_x": 0,
            "start_y": 0,
            "end_x": 900,
            "end_y": 0,
        },
    )
    state = AccessibilityState("state-1", "com.example.Editor", "Editor", DesktopRect(100, 50, 800, 600), (), False)
    session.bridge._observations.remember(state, drag)
    return AuthenticatedToDeviceEvent(
        source={"content": drag.to_content()},
        sender=_CONTROLLER.user_id,
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice(
            _CONTROLLER.user_id,
            _CONTROLLER.device_id,
            "curve",
            _CONTROLLER.ed25519,
        ),
    )


@pytest.mark.asyncio
async def test_close_bounds_a_stuck_native_action_and_its_late_return_neither_records_nor_sends_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close waits a bounded time for a hung drag, then releases the run; the drag's late return writes and sends nothing.

    The drag returns just as the journal closes, the last moment its command could still be recorded, and its
    command stays started, so the next run reports its outcome as unknown.
    """
    provider, _, _ = real_provider()
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    posted: list[dict[str, object]] = []
    quartz = fake_quartz(posted, [window(42, 0, 0, 2000)])
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    reached, release, released_button = threading.Event(), threading.Event(), threading.Event()

    def post(_tap: int, event: dict[str, object]) -> None:
        posted.append(event)
        if event["kind"] == 2:
            released_button.set()

    def sleep(_delay: float) -> None:
        if not reached.is_set():
            reached.set()
            assert release.wait(5)

    def return_the_drag() -> None:
        release.set()
        assert released_button.wait(5)

    quartz.CGEventPost = post
    monkeypatch.setattr("mindroom.desktop.macos_input.time.sleep", sleep)
    monkeypatch.setattr("mindroom.desktop.bridge_session.PyAutoGuiDesktopProvider", lambda **_kwargs: provider)
    monkeypatch.setattr("mindroom.desktop.bridge._run_macos_application_events", asyncio.Event().wait)
    monkeypatch.setattr("mindroom.desktop.bridge._STOP_ACTIVE_ACTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.bridge.resolve_pinned_device", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", AsyncMock())
    session = await _start(
        tmp_path,
        _config(apps=("com.example.Editor",), shell=False),
        offline_session_deps(FakeMatrixOwner()),
        control_lease_seconds=600,
    )
    late_writes = _record_late_journal_writes(session.bridge._journal, monkeypatch, return_the_drag)
    try:
        await session.bridge.on_to_device_event(_drag_event(session))
        assert await asyncio.to_thread(reached.wait, 5)
        await asyncio.wait_for(session.close(), timeout=5)
        # Give a coroutine still waiting on the drag every chance to record its late result.
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        release.set()

    assert not session.live
    assert released_button.is_set()
    assert late_writes == []
    # Pointer moved, pressed, took one drag step, then sent only the release once control ended.
    assert [event["kind"] for event in posted] == [8, 1, 3, 2]
    reopened = DesktopCommandJournal.load(
        desktop_journal_path(tmp_path),
        controller_key=controller_journal_key(_CONTROLLER),
    )
    try:
        entry = reopened.get("drag-1")
        assert entry is not None
        assert (entry.state, entry.response) == ("started", None)
    finally:
        reopened.close()
