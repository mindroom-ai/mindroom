"""Native desktop host lifecycle tests."""

# Compact fakes keep the wire-level lifecycle assertions readable.
# ruff: noqa: C416, D101, D102, D103, EM101, S106, TRY003

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import signal
import sys
import time
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent

from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.command_journal import DesktopCommandJournal, DesktopCommandJournalError
from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError
from mindroom.desktop.native_config import (
    NativeDesktopConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)
from mindroom.desktop.native_host import (
    NativeBridgeRuntime,
    NativeDesktopHost,
    NativeHostDependencies,
    serve_native_stream,
    supervise_native_tasks,
)
from mindroom.desktop.native_protocol import NativeProtocolError, NativeRequest, parse_native_request
from mindroom.desktop.protocol import DESKTOP_COMMAND_EVENT_TYPE, DesktopCommand
from mindroom.desktop.session import DesktopMatrixSession, save_desktop_session
from mindroom.desktop.shell import DesktopShell, DesktopShellRequest
from mindroom.file_locks import advisory_file_lock, file_lock_is_held

if TYPE_CHECKING:
    from collections.abc import Buffer
    from pathlib import Path


def _config_payload() -> dict[str, object]:
    return {
        "v": 1,
        "revision": 0,
        "enabled": True,
        "controller": {"user_id": "@controller:example.org", "device_id": "DEVICE", "ed25519": "key"},
        "allowed_requester_ids": ["@person:example.org"],
        "allowed_agent_names": ["assistant"],
        "allowed_app_ids": ["com.example.Editor"],
        "capture": {"max_screenshot_width": 1568, "jpeg_quality": 80},
        "browser": {
            "enabled": False,
            "executable_path": None,
            "user_data_dir": None,
            "timeout_seconds": 90,
        },
    }


class FakeRuntime:
    def __init__(self) -> None:
        self.running = False
        self.stopped = 0
        self.grants: list[int] = []
        self.revoked = 0
        self.reset = 0
        self.shell_calls: list[tuple[str, dict[str, object]]] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self.stopped += 1

    def status(self) -> dict[str, object]:
        return {
            "mode": "observe_only" if self.running else "stopped",
            "control_available": False,
            "lease_remaining_seconds": 0,
            "lease_expires_at_ms": None,
            "emergency_stop_latched": False,
            "active_action": None,
        }

    def grant_control(self, duration_seconds: int) -> dict[str, object]:
        self.grants.append(duration_seconds)
        return self.status()

    def revoke_control(self) -> dict[str, object]:
        self.revoked += 1
        return self.status()

    def reset_emergency_stop(self) -> dict[str, object]:
        self.reset += 1
        return self.status()

    def decide_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]:
        parameters = {
            "command_id": command_id,
            "approved": approved,
            "auto_approve_seconds": auto_approve_seconds,
            "auto_approve_until_revoked": auto_approve_until_revoked,
        }
        self.shell_calls.append(("decide_shell", parameters))
        return self.status()

    def grant_shell(self, duration_seconds: int | None = None, *, until_revoked: bool = False) -> dict[str, object]:
        self.shell_calls.append(("grant_shell", {"duration_seconds": duration_seconds, "until_revoked": until_revoked}))
        return self.status()

    def revoke_shell(self) -> dict[str, object]:
        self.shell_calls.append(("revoke_shell", {}))
        return self.status()

    def kill_shell_handle(self, handle: str) -> dict[str, object]:
        self.shell_calls.append(("kill_shell_handle", {"handle": handle}))
        return self.status()

    async def connect_browser(self) -> None:
        pass

    async def disconnect_browser(self) -> None:
        pass


def _request(action: str, **parameters: object) -> NativeRequest:
    return NativeRequest(str(uuid4()), action, parameters)


def _local_access_request(revision: int, roots: list[str], *, enabled: bool) -> NativeRequest:
    """Build local capability edit without interpreting shell as subprocess execution."""
    request = _request("set_local_access", expected_revision=revision, files={"roots": roots})
    request.parameters["shell"] = {"enabled": enabled}
    return request


def test_status_restores_saved_identity_without_exposing_or_changing_session(tmp_path: Path) -> None:
    path = tmp_path / "desktop_bridge" / "matrix_session.json"
    save_desktop_session(path, DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret-token"))
    original = path.read_bytes()
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")

    status = host.status()

    assert status["pairing"] == {
        "state": "unpaired",
        "session_state": "ready",
        "homeserver": "https://example.org",
        "user_id": "@me:example.org",
        "device_id": "LOCAL",
        "controller_fingerprint": None,
    }
    assert status["bridge"]["state"] == "stopped"
    assert "secret-token" not in repr(status)
    assert path.read_bytes() == original


def test_status_detects_session_created_and_removed_while_helper_runs(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    path = tmp_path / "desktop_bridge" / "matrix_session.json"
    assert host.status()["pairing"]["session_state"] == "missing"

    save_desktop_session(path, DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret-token"))

    assert host.status()["pairing"]["device_id"] == "LOCAL"
    path.unlink()
    assert host.status()["pairing"]["session_state"] == "missing"
    assert host.status()["pairing"]["device_id"] is None


@pytest.mark.parametrize("invalid", ["malformed", "exposed", "directory"])
def test_status_keeps_invalid_saved_session_recoverable_without_trusting_identity(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "desktop_bridge" / "matrix_session.json"
    save_desktop_session(path, DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret-token"))
    if invalid == "malformed":
        path.write_text("invalid-secret-json")
    elif invalid == "exposed":
        path.chmod(0o644)
    else:
        path.unlink()
        path.mkdir()
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")

    status = host.status()

    assert status["pairing"]["session_state"] == "invalid"
    assert status["pairing"]["device_id"] is None
    assert status["pairing"]["user_id"] is None
    assert "secret" not in repr(status)
    assert status["helper"]["state"] == "running"
    assert path.exists()


def test_status_does_not_open_a_fifo_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "desktop_bridge" / "matrix_session.json"
    path.parent.mkdir()
    os.mkfifo(path, 0o600)
    open_file = os.open

    def guarded_open(file: Path, flags: int) -> int:
        if file == path:
            assert flags & os.O_NONBLOCK, "Opening the session FIFO would block all helper requests"
        return open_file(file, flags)

    monkeypatch.setattr(os, "open", guarded_open)
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")

    assert host.status()["pairing"]["session_state"] == "invalid"


def test_replacing_directory_session_rejects_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "desktop_bridge" / "matrix_session.json"
    path.mkdir(parents=True)
    sentinel = path / "keep.txt"
    sentinel.write_text("keep")
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")

    async def unexpected_authentication(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Authentication must not start for an unreplaceable session path")

    monkeypatch.setattr("mindroom.desktop.session.resolve_desktop_login_method", unexpected_authentication)

    with pytest.raises(NativeProtocolError, match="regular file"):
        asyncio.run(host.handle(_request("login", homeserver="https://example.org", replace=True)))

    assert sentinel.read_text() == "keep"


def test_host_configure_start_control_and_shutdown(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    dependencies = NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime)
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1.2.3",
        dependencies=dependencies,
    )

    configured = asyncio.run(
        host.handle(_request("configure", expected_revision=0, config=_config_payload())),
    )
    assert configured["status"]["config"]["state"] == "ready"
    assert configured["status"]["config"]["revision"] == 1
    assert configured["status"]["config"]["enabled"] is True
    asyncio.run(host.handle(_request("start")))
    asyncio.run(host.handle(_request("grant_control", duration_seconds=600)))
    asyncio.run(host.handle(_request("revoke_control")))
    asyncio.run(host.handle(_request("reset_emergency_stop")))
    asyncio.run(host.shutdown())

    assert runtime.grants == [600]
    assert runtime.revoked == 1
    assert runtime.reset == 1
    assert runtime.stopped == 1


@pytest.mark.asyncio
async def test_external_setup_does_not_change_running_authority(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None),
        helper_version="test",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
    )
    await host.handle(_request("configure", expected_revision=0, config=_config_payload()))
    await host.handle(_request("start"))
    path = native_config_path(tmp_path)
    original = load_native_config(path)
    save_native_config(path, original.with_allowed_apps(["com.example.Other"]), expected_revision=original.revision)
    assert host.status()["config"]["allowed_app_ids"] == ["com.example.Editor"]
    await host.handle(_request("stop"))
    assert host.status()["config"]["allowed_app_ids"] == ["com.example.Other"]


def test_host_status_returns_persisted_browser_settings(tmp_path: Path) -> None:
    executable = tmp_path / "browser"
    executable.touch()
    profile = tmp_path / "profile"
    profile.mkdir()
    payload = _config_payload()
    payload["browser"] = {
        "enabled": True,
        "executable_path": str(executable),
        "user_data_dir": str(profile),
        "timeout_seconds": 90,
    }
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: FakeRuntime()),
    )

    asyncio.run(host.handle(_request("configure", expected_revision=0, config=payload)))

    browser = cast("dict[str, object]", host.status()["browser"])
    assert browser["configured"] is True
    assert browser["executable_path"] == str(executable)
    assert browser["user_data_dir"] == str(profile)


@pytest.mark.parametrize("existing_config", [False, True])
@pytest.mark.parametrize(
    ("field", "value"),
    [("user_id", "@other:example.org"), ("device_id", "OTHER"), ("ed25519", "other-key")],
)
def test_configure_rejects_controller_changes_without_altering_owned_work(
    tmp_path: Path,
    existing_config: bool,
    field: str,
    value: str,
) -> None:
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
    )
    config_path = tmp_path / "desktop_bridge" / "native_config.json"
    if existing_config:
        asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    original_config = config_path.read_bytes() if existing_config else None
    journal_path = tmp_path / "desktop_bridge" / "commands.sqlite3"
    journal = DesktopCommandJournal.load(
        journal_path,
        controller_key='["@controller:example.org", "DEVICE", "key"]',
    )
    command = DesktopCommand("request", "session", 1, 1000, 31000, "status", "@person:example.org", "assistant")
    journal.admit(command, "a" * 64)
    journal.close()
    original_journal = journal_path.read_bytes()
    payload = _config_payload()
    payload["revision"] = int(existing_config)
    cast("dict[str, str]", payload["controller"])[field] = value

    with pytest.raises(NativeProtocolError, match="different controller") as caught:
        asyncio.run(host.handle(_request("configure", expected_revision=int(existing_config), config=payload)))

    assert caught.value.code == "invalid_request"
    assert journal_path.read_bytes() == original_journal
    assert config_path.exists() == existing_config
    if existing_config:
        assert config_path.read_bytes() == original_config
    assert host.status()["config"]["revision"] == int(existing_config)


def test_configure_same_controller_preserves_journal_and_saves_app_changes(tmp_path: Path) -> None:
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
    )
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    journal_path = tmp_path / "desktop_bridge" / "commands.sqlite3"
    journal = DesktopCommandJournal.load(
        journal_path,
        controller_key='["@controller:example.org", "DEVICE", "key"]',
    )
    journal.close()
    original_journal = journal_path.read_bytes()
    payload = _config_payload()
    payload.update(revision=1, allowed_app_ids=["com.example.Other"])

    result = asyncio.run(host.handle(_request("configure", expected_revision=1, config=payload)))

    assert result["status"]["config"]["revision"] == 2
    assert result["status"]["config"]["allowed_app_ids"] == ["com.example.Other"]
    assert journal_path.read_bytes() == original_journal


def test_set_allowed_apps_preserves_connection_and_other_settings(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None), helper_version="1")
    payload = _config_payload()
    payload["capture"] = {"max_screenshot_width": 1200, "jpeg_quality": 65}
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=payload)))
    original = load_native_config(native_config_path(tmp_path)).to_payload()

    result = asyncio.run(
        host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=["com.example.Other"])),
    )

    saved = load_native_config(native_config_path(tmp_path)).to_payload()
    assert saved == original | {"revision": 2, "allowed_app_ids": ["com.example.Other"]}
    assert result["status"]["config"]["allowed_app_ids"] == ["com.example.Other"]
    with pytest.raises(NativeProtocolError, match="changed"):
        asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=[])))
    assert load_native_config(native_config_path(tmp_path)).to_payload() == saved


def test_local_access_save_is_scoped_and_other_saves_preserve_it(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    root = tmp_path / "selected"
    root.mkdir()
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    before = load_native_config(native_config_path(tmp_path)).to_payload()
    response = asyncio.run(host.handle(_local_access_request(1, [str(root)], enabled=True)))
    assert response["status"]["config"]["file_roots"] == [str(root.resolve())]
    assert response["status"]["config"]["shell_enabled"] is True
    assert response["status"]["shell"] == {
        "enabled": True,
        "pending": None,
        "auto_approve_remaining_seconds": 0.0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert load_native_config(native_config_path(tmp_path)).to_payload() == before | {
        "revision": 2,
        "files": {"roots": [str(root.resolve())]},
        "shell": {"enabled": True},
    }
    # A disappeared saved root must not block unrelated edits.
    root.rmdir()
    asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=2, allowed_app_ids=[])))
    browser = {"enabled": False, "executable_path": None, "user_data_dir": None}
    asyncio.run(host.handle(_request("set_browser_config", expected_revision=3, browser=browser)))
    old_payload = _config_payload()
    old_payload["revision"] = 4
    asyncio.run(host.handle(_request("configure", expected_revision=4, config=old_payload)))
    saved = load_native_config(native_config_path(tmp_path))
    assert saved.files.roots == (root.resolve(),)
    assert saved.shell.enabled is True
    unchanged = native_config_path(tmp_path).read_bytes()
    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_local_access_request(4, [], enabled=False)))
    assert caught.value.code == "revision_conflict"
    assert native_config_path(tmp_path).read_bytes() == unchanged


def test_changed_local_root_must_exist_and_new_controller_gets_no_local_access(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    root = tmp_path / "selected"
    root.mkdir()
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    asyncio.run(host.handle(_local_access_request(1, [str(root)], enabled=True)))
    unchanged = native_config_path(tmp_path).read_bytes()
    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_local_access_request(2, [str(root), str(tmp_path / "missing")], enabled=True)))
    assert caught.value.code == "invalid_request"
    assert native_config_path(tmp_path).read_bytes() == unchanged
    payload = _config_payload()
    payload["revision"] = 2
    payload["controller"] = {"user_id": "@other:example.org", "device_id": "OTHER", "ed25519": "other-key"}
    asyncio.run(host.handle(_request("configure", expected_revision=2, config=payload)))
    saved = load_native_config(native_config_path(tmp_path))
    assert saved.files.roots == ()
    assert saved.shell.enabled is False


def test_configure_with_local_access_validates_only_new_roots(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    saved_root = tmp_path / "saved"
    saved_root.mkdir()
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    asyncio.run(host.handle(_local_access_request(1, [str(saved_root)], enabled=False)))
    saved_root.rmdir()
    payload = _config_payload() | {"revision": 2, "files": {"roots": [str(saved_root.resolve())]}}
    payload["shell"] = {"enabled": True}
    asyncio.run(host.handle(_request("configure", expected_revision=2, config=payload)))
    assert load_native_config(native_config_path(tmp_path)).shell.enabled is True
    payload = payload | {"revision": 3, "files": {"roots": [str(tmp_path / "missing")]}}
    with pytest.raises(NativeProtocolError, match="existing directory"):
        asyncio.run(host.handle(_request("configure", expected_revision=3, config=payload)))


def test_set_local_access_requires_saved_config_stopped_bridge_and_exact_fields(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_local_access_request(0, [], enabled=True)))
    assert caught.value.code == "configuration_missing"
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    unchanged = native_config_path(tmp_path).read_bytes()
    for request in (
        _request("set_local_access", expected_revision=1, files={"roots": []}),
        _local_access_request(1, [], enabled="yes"),
        _local_access_request(1, ["relative"], enabled=False),
    ):
        with pytest.raises(NativeProtocolError) as caught:
            asyncio.run(host.handle(request))
        assert caught.value.code == "invalid_request"
    extra = _local_access_request(1, [], enabled=True)
    extra.parameters["allowed_app_ids"] = []
    with pytest.raises(NativeProtocolError, match="missing or unsupported"):
        asyncio.run(host.handle(extra))
    host._runtime = FakeRuntime()
    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_local_access_request(1, [], enabled=True)))
    assert caught.value.code == "busy"
    assert native_config_path(tmp_path).read_bytes() == unchanged


def test_folder_only_config_starts_without_app_selection(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
    )
    root = tmp_path / "selected"
    root.mkdir()
    payload = _config_payload() | {"allowed_app_ids": [], "files": {"roots": [str(root)]}}
    payload["shell"] = {"enabled": False}
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=payload)))
    assert asyncio.run(host.handle(_request("start")))["status"]["bridge"]["state"] == "observe_only"


@pytest.fixture
def bridge_transport(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Accept the pinned controller and capture encrypted bridge responses."""
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.bridge.resolve_pinned_device", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", send)
    return send


async def _shell_host(tmp_path: Path) -> NativeDesktopHost:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None), helper_version="1")
    payload = _config_payload() | {"allowed_app_ids": [], "files": {"roots": []}}
    payload["shell"] = {"enabled": True}
    await host.handle(_request("configure", expected_revision=0, config=payload))
    return host


def _attach_shell_runtime(host: NativeDesktopHost, tmp_path: Path) -> NativeBridgeRuntime:
    """Attach a real shell-only bridge to the native channel without opening Matrix."""
    config = load_native_config(native_config_path(tmp_path))
    runtime = NativeBridgeRuntime(SimpleNamespace(storage_root=tmp_path), config)
    runtime._shell = DesktopShell(environment={"PATH": os.defpath})
    runtime._bridge = DesktopBridge(
        client=object(),
        provider=None,
        policy=DesktopBridgePolicy(
            controller=config.controller,
            allowed_requester_ids=frozenset(config.allowed_requester_ids),
            allowed_agent_names=frozenset(config.allowed_agent_names),
            allowed_app_ids=frozenset(),
            shell_enabled=True,
        ),
        shell=runtime._shell,
        journal_path=tmp_path / "desktop_bridge" / "commands.sqlite3",
    )
    host._runtime = runtime
    return runtime


def _shell_event(
    command: str,
    cwd: Path,
    *,
    request_id: str = "shell-1",
    sequence: int = 1,
    action: str = "run_shell",
    parameters: dict[str, object] | None = None,
) -> AuthenticatedToDeviceEvent:
    now_ms = round(time.time() * 1000)
    content = DesktopCommand(
        request_id,
        "session",
        sequence,
        now_ms,
        now_ms + 60_000,
        action,
        "@person:example.org",
        "assistant",
        {"command": command, "cwd": str(cwd)} if parameters is None else parameters,
    ).to_content()
    return AuthenticatedToDeviceEvent(
        source={"content": content},
        sender="@controller:example.org",
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice("@controller:example.org", "DEVICE", "curve-key", "key"),
    )


async def _wait_for_native_pending(host: NativeDesktopHost) -> dict[str, object]:
    for _ in range(200):
        pending = host.status()["shell"]["pending"]
        if pending is not None:
            return pending
        await asyncio.sleep(0.005)
    pytest.fail("shell approval never reached native status")


@pytest.mark.asyncio
async def test_native_decision_runs_exact_pending_shell_command_once(
    bridge_transport: AsyncMock,
    tmp_path: Path,
) -> None:
    host = await _shell_host(tmp_path)
    bridge = _attach_shell_runtime(host, tmp_path)._bridge
    event = _shell_event("printf ran >> marker", tmp_path)
    await bridge.on_to_device_event(event)
    execution = asyncio.create_task(bridge.execute_pending(shell_starts=True))
    pending = await _wait_for_native_pending(host)
    assert {key: pending[key] for key in ("request_id", "requester_id", "agent_name", "command", "cwd")} == {
        "request_id": "shell-1",
        "requester_id": "@person:example.org",
        "agent_name": "assistant",
        "command": "printf ran >> marker",
        "cwd": str(tmp_path),
    }
    for parameters, code in (
        ({"command_id": "shell-2", "approved": True, "auto_approve_seconds": 0}, "shell_denied"),
        ({"command_id": "shell-1", "approved": "true", "auto_approve_seconds": 0}, "invalid_request"),
        ({"command_id": "shell-1", "approved": True, "auto_approve_seconds": 30}, "invalid_request"),
        ({"command_id": "shell-1", "approved": True, "auto_approve_seconds": True}, "invalid_request"),
        ({"command_id": "shell-1", "approved": False, "auto_approve_seconds": 60}, "invalid_request"),
        ({"command_id": "shell-1", "approved": True}, "invalid_request"),
        (
            {"command_id": "shell-1", "approved": True, "auto_approve_seconds": 60, "auto_approve_until_revoked": True},
            "invalid_request",
        ),
        (
            {"command_id": "shell-1", "approved": False, "auto_approve_seconds": 0, "auto_approve_until_revoked": True},
            "invalid_request",
        ),
        (
            {"command_id": "shell-1", "approved": True, "auto_approve_seconds": 0, "auto_approve_until_revoked": 1},
            "invalid_request",
        ),
        ({"command_id": "shell-1", "approved": True, "auto_approve_seconds": 0, "extra": True}, "invalid_request"),
    ):
        with pytest.raises(NativeProtocolError) as caught:
            await host.handle(_request("decide_shell", **parameters))
        assert caught.value.code == code
    assert not (tmp_path / "marker").exists()

    await host.handle(_request("decide_shell", command_id="shell-1", approved=True, auto_approve_seconds=0))
    await execution
    await bridge.deliver_pending()
    assert (tmp_path / "marker").read_text() == "ran"
    completed = bridge_transport.await_args.kwargs["content"]
    assert completed["result"]["exit_code"] == 0
    with pytest.raises(NativeProtocolError) as caught:
        await host.handle(_request("decide_shell", command_id="shell-1", approved=True, auto_approve_seconds=0))
    assert caught.value.code == "shell_denied"
    assert host.status()["shell"]["auto_approve_remaining_seconds"] == 0.0

    stopped = await host.handle(_request("stop"))
    assert stopped["status"]["bridge"]["state"] == "stopped"
    assert stopped["status"]["shell"]["pending"] is None
    with pytest.raises(NativeProtocolError) as caught:
        await host.handle(_request("decide_shell", command_id="shell-1", approved=True, auto_approve_seconds=0))
    assert caught.value.code == "not_running"

    restarted = _attach_shell_runtime(host, tmp_path)._bridge
    await restarted.on_to_device_event(event)
    await restarted.execute_pending(shell_starts=True)
    await restarted.deliver_pending()
    assert bridge_transport.await_args.kwargs["content"] == completed
    assert (tmp_path / "marker").read_text() == "ran"
    await host.shutdown()


@pytest.mark.asyncio
async def test_native_shell_grant_is_local_bounded_and_revocable(bridge_transport: AsyncMock, tmp_path: Path) -> None:
    host = await _shell_host(tmp_path)
    bridge = _attach_shell_runtime(host, tmp_path)._bridge
    for parameters in (
        {"duration_seconds": 59},
        {"duration_seconds": 3601},
        {"duration_seconds": True},
        {"duration_seconds": "900"},
        {"until_revoked": False},
        {"until_revoked": 1},
        {"duration_seconds": 900, "until_revoked": True},
        {},
    ):
        with pytest.raises(NativeProtocolError) as caught:
            await host.handle(_request("grant_shell", **parameters))
        assert caught.value.code == "invalid_request"
    granted = await host.handle(_request("grant_shell", duration_seconds=900))
    assert 899 < granted["status"]["shell"]["auto_approve_remaining_seconds"] <= 900
    await bridge.on_to_device_event(_shell_event("printf granted > granted", tmp_path))
    await bridge.execute_pending(shell_starts=True)
    assert (tmp_path / "granted").read_text() == "granted"

    revoked = await host.handle(_request("revoke_shell"))
    assert revoked["status"]["shell"]["auto_approve_remaining_seconds"] == 0.0
    await bridge.on_to_device_event(_shell_event("touch after-revoke", tmp_path, request_id="shell-2", sequence=2))
    execution = asyncio.create_task(bridge.execute_pending(shell_starts=True))
    await _wait_for_native_pending(host)
    assert not (tmp_path / "after-revoke").exists()
    await host.handle(_request("revoke_shell"))
    await execution
    await bridge.deliver_pending()
    assert "did not run" in bridge_transport.await_args.kwargs["content"]["error"]
    assert not (tmp_path / "after-revoke").exists()

    forever = await host.handle(_request("grant_shell", until_revoked=True))
    assert forever["status"]["shell"]["auto_approve_until_revoked"] is True
    assert forever["status"]["shell"]["auto_approve_remaining_seconds"] == 0.0
    await bridge.on_to_device_event(_shell_event("printf kept > kept", tmp_path, request_id="shell-3", sequence=3))
    await bridge.execute_pending(shell_starts=True)
    assert (tmp_path / "kept").read_text() == "kept"
    revoked = await host.handle(_request("revoke_shell"))
    assert revoked["status"]["shell"]["auto_approve_until_revoked"] is False
    await host.shutdown()


@pytest.mark.asyncio
async def test_native_status_lists_handles_and_can_kill_one(bridge_transport: AsyncMock, tmp_path: Path) -> None:
    host = await _shell_host(tmp_path)
    bridge = _attach_shell_runtime(host, tmp_path)._bridge
    await host.handle(_request("grant_shell", duration_seconds=60))
    pid_file = tmp_path / "leader.pid"
    command = f"echo $$ > {pid_file}; sleep 30"
    event = _shell_event("", tmp_path, parameters={"command": command, "cwd": str(tmp_path), "timeout_seconds": 1})
    try:
        await bridge.on_to_device_event(event)
        await bridge.execute_pending(shell_starts=True)
        await bridge.deliver_pending()
        handle = bridge_transport.await_args.kwargs["content"]["result"]["handle"]
        [entry] = host.status()["shell"]["handles"]
        assert {key: entry[key] for key in ("handle", "requester_id", "agent_name", "command_preview", "state")} == {
            "handle": handle,
            "requester_id": "@person:example.org",
            "agent_name": "assistant",
            "command_preview": command,
            "state": "running",
        }
        assert entry["elapsed_seconds"] >= 1
        for parameters, code in (({}, "invalid_request"), ({"handle": ""}, "invalid_request")):
            with pytest.raises(NativeProtocolError) as caught:
                await host.handle(_request("kill_shell_handle", **parameters))
            assert caught.value.code == code
        with pytest.raises(NativeProtocolError) as caught:
            await host.handle(_request("kill_shell_handle", handle="shell:missing"))
        assert caught.value.code == "shell_denied"

        killed = await host.handle(_request("kill_shell_handle", handle=handle))
        assert killed["status"]["shell"]["handles"] == []
        leader = int(pid_file.read_text())
        for _ in range(400):
            try:
                os.kill(leader, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("locally killed handle kept running")
    finally:
        if pid_file.exists() and pid_file.read_text().strip():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
    await host.shutdown()


class _RecordingOutput(io.BytesIO):
    """Record each native response with the shell's active command at the moment it is written."""

    def __init__(self, shell: DesktopShell) -> None:
        super().__init__()
        self.shell = shell
        self.responses: list[tuple[dict[str, object], object]] = []

    def write(self, data: Buffer, /) -> int:
        for line in bytes(data).splitlines():
            message = json.loads(line)
            if message["type"] == "response":
                self.responses.append((message, self.shell.status()["active_request_id"]))
        return super().write(data)


@pytest.mark.asyncio
async def test_revoke_shell_keeps_native_channel_responsive_while_command_stops(
    bridge_transport: AsyncMock,
    tmp_path: Path,
) -> None:
    host = await _shell_host(tmp_path)
    runtime = _attach_shell_runtime(host, tmp_path)
    shell, bridge = runtime._shell, runtime._bridge
    await host.handle(_request("grant_shell", duration_seconds=60))
    script = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} >/dev/null 2>&1 & wait"
    event = _shell_event(command, tmp_path)
    await bridge.on_to_device_event(event)
    execution = asyncio.create_task(bridge.execute_pending(shell_starts=True))
    pid_file = tmp_path / "child.pid"
    try:
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.005)
        child = int(pid_file.read_text())
        output = _RecordingOutput(shell)
        records = b"".join(
            json.dumps({"v": 1, "request_id": str(uuid4()), "action": action, "parameters": {}}).encode() + b"\n"
            for action in ("revoke_shell", "status", "stop")
        )
        await asyncio.wait_for(
            serve_native_stream(host, input_stream=io.BytesIO(records), output_stream=output),
            timeout=5,
        )
        await asyncio.wait_for(execution, timeout=1)
        (revoked, revoke_active), (status, status_active), (stopped, _) = output.responses
        # Both replies precede the end of termination: the TERM-resistant child still holds the command open.
        assert (revoke_active, status_active) == ("shell-1", "shell-1")
        assert revoked["ok"] is True
        assert status["result"]["status"]["shell"]["auto_approve_remaining_seconds"] == 0.0
        assert stopped["ok"] is True
        assert stopped["result"]["status"]["bridge"]["state"] == "stopped"
        restarted = _attach_shell_runtime(host, tmp_path)._bridge
        await restarted.on_to_device_event(event)
        await restarted.deliver_pending()
        assert "was stopped" in bridge_transport.await_args.kwargs["content"]["error"]
        await host.shutdown()
        for _ in range(400):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("TERM-resistant child survived revoke and stop")
    finally:
        if pid_file.exists() and pid_file.read_text():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.parametrize(
    ("action", "parameters", "call"),
    [
        (
            "decide_shell",
            {"command_id": "shell-1", "approved": False, "auto_approve_seconds": 0},
            {
                "command_id": "shell-1",
                "approved": False,
                "auto_approve_seconds": 0,
                "auto_approve_until_revoked": False,
            },
        ),
        (
            "decide_shell",
            {"command_id": "shell-1", "approved": True, "auto_approve_seconds": 0, "auto_approve_until_revoked": True},
            {"command_id": "shell-1", "approved": True, "auto_approve_seconds": 0, "auto_approve_until_revoked": True},
        ),
        ("grant_shell", {"duration_seconds": 60}, {"duration_seconds": 60, "until_revoked": False}),
        ("grant_shell", {"until_revoked": True}, {"duration_seconds": None, "until_revoked": True}),
        ("revoke_shell", {}, {}),
        ("kill_shell_handle", {"handle": "shell:0123abcd"}, {"handle": "shell:0123abcd"}),
    ],
)
def test_shell_controls_bypass_lifecycle_lock_and_require_running_bridge(
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
    call: dict[str, object],
) -> None:
    async def scenario() -> list[tuple[str, dict[str, object]]]:
        runtime = FakeRuntime()
        host = NativeDesktopHost(
            SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
            helper_version="1",
            dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
        )
        await host.handle(_request("configure", expected_revision=0, config=_config_payload()))
        with pytest.raises(NativeProtocolError) as caught:
            await host.handle(_request(action, **parameters))
        assert caught.value.code == "not_running"
        await host.handle(_request("start"))
        await host._lock.acquire()
        try:
            await asyncio.wait_for(host.handle(_request(action, **parameters)), timeout=0.1)
        finally:
            host._lock.release()
        await host.shutdown()
        return runtime.shell_calls

    assert asyncio.run(scenario()) == [(action, call)]


class _FakeOwner:
    """Owned Matrix session stand-in that records callback registration and closing."""

    def __init__(self) -> None:
        self.client = SimpleNamespace(to_device_callbacks=[], add_to_device_callback=self._register)
        self.source = object()
        self.closed = False

    def _register(self, callback: object, event_type: object) -> None:
        self.client.to_device_callbacks.append((callback, event_type))

    async def close(self) -> None:
        self.closed = True


class _IdleTransport:
    def __init__(self, _source: object, *, wait_for_capacity: object) -> None:
        del wait_for_capacity

    async def run(self) -> None:
        await asyncio.Event().wait()


@pytest.fixture
def offline_runtime_session(monkeypatch: pytest.MonkeyPatch) -> _FakeOwner:
    """Start the native runtime without Matrix, and fail if it builds the GUI provider."""
    owner = _FakeOwner()

    async def open_client(*_args: object, **_kwargs: object) -> _FakeOwner:
        return owner

    def forbidden_gui_provider(**_kwargs: object) -> None:
        pytest.fail("GUI provider constructed without application access")

    session = SimpleNamespace(cloudflare_access=False, homeserver="https://example.org")
    monkeypatch.setattr("mindroom.desktop.session.load_desktop_session", lambda _path: session)
    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.session.prepare_desktop_client", AsyncMock())
    monkeypatch.setattr("mindroom.matrix.olm_to_device.resolve_pinned_device", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.transport.DesktopTransport", _IdleTransport)
    monkeypatch.setattr("mindroom.desktop.provider.PyAutoGuiDesktopProvider", forbidden_gui_provider)
    monkeypatch.setattr(
        "mindroom.desktop.login_environment.capture_login_environment",
        AsyncMock(return_value={"PATH": os.defpath, "MINDROOM_CAPTURED": "from-login-shell"}),
    )
    return owner


def _folder_and_shell_config(root: Path) -> NativeDesktopConfig:
    payload = _config_payload() | {"allowed_app_ids": [], "files": {"roots": [str(root)]}}
    payload["shell"] = {"enabled": True}
    return NativeDesktopConfig.from_payload(payload)


@pytest.mark.asyncio
async def test_runtime_starts_folder_and_shell_access_without_gui_provider(
    offline_runtime_session: _FakeOwner,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    runtime = NativeBridgeRuntime(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None),
        _folder_and_shell_config(root),
    )
    await runtime.start()
    filesystem = runtime._filesystem
    status = runtime.status()
    assert status["gui_available"] is False
    assert [folder["path"] for folder in status["file_roots"]] == [str(root)]
    assert status["shell"]["enabled"] is True
    # The shell runtime runs commands with the environment captured from the login shell at start.
    runtime.grant_shell(60)
    request = DesktopShellRequest(
        "captured",
        "@person:example.org",
        "assistant",
        'printf "$MINDROOM_CAPTURED"',
        str(tmp_path),
        round(time.time() * 1000) + 60_000,
    )
    result = await runtime._shell.execute(request)
    assert result.output.read() == b"from-login-shell"
    result.output.release()
    await runtime.stop()
    assert offline_runtime_session.closed is True
    assert offline_runtime_session.client.to_device_callbacks == []
    with pytest.raises(DesktopFilesystemError, match="closed"):
        filesystem.list_folders()
    assert runtime.status()["mode"] == "stopped"


@pytest.mark.asyncio
async def test_runtime_start_failure_releases_pinned_folders(
    offline_runtime_session: _FakeOwner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    opened: list[DesktopFilesystem] = []

    class RecordingFilesystem(DesktopFilesystem):
        def __init__(self, roots: tuple[Path, ...]) -> None:
            super().__init__(roots)
            opened.append(self)

    monkeypatch.setattr("mindroom.desktop.filesystem.DesktopFilesystem", RecordingFilesystem)
    (tmp_path / "desktop_bridge" / "commands.sqlite3").mkdir(parents=True)
    runtime = NativeBridgeRuntime(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None),
        _folder_and_shell_config(root),
    )
    with pytest.raises(DesktopCommandJournalError):
        await runtime.start()
    assert len(opened) == 1
    with pytest.raises(DesktopFilesystemError, match="closed"):
        opened[0].list_folders()
    assert offline_runtime_session.closed is True
    assert runtime.status()["mode"] == "stopped"


def test_set_allowed_apps_requires_saved_configuration_and_stopped_access(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
    )
    with pytest.raises(NativeProtocolError, match="setup"):
        asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=0, allowed_app_ids=[])))
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    asyncio.run(host.handle(_request("start")))
    with pytest.raises(NativeProtocolError, match="Stop"):
        asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=[])))
    asyncio.run(host.handle(_request("stop")))
    result = asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=[])))
    assert result["status"]["config"]["allowed_app_ids"] == []
    assert result["status"]["bridge"]["state"] == "stopped"
    with pytest.raises(NativeProtocolError, match="at least one local capability"):
        asyncio.run(host.handle(_request("start")))


@pytest.mark.parametrize("app_ids", ["com.example.Editor", [""], [123]])
def test_set_allowed_apps_validates_app_ids(tmp_path: Path, app_ids: object) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None), helper_version="1")
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    with pytest.raises(NativeProtocolError):
        asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=app_ids)))
    assert host.status()["config"]["revision"] == 1


def test_app_only_save_preserves_browser_config_when_paths_disappear(tmp_path: Path) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None), helper_version="1")
    executable = tmp_path / "browser"
    executable.touch()
    profile = tmp_path / "profile"
    profile.mkdir()
    payload = _config_payload()
    payload["browser"] = {
        "enabled": True,
        "executable_path": str(executable),
        "user_data_dir": str(profile),
        "timeout_seconds": 45,
    }
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=payload)))
    executable.unlink()
    profile.rmdir()

    asyncio.run(host.handle(_request("set_allowed_apps", expected_revision=1, allowed_app_ids=[])))

    saved = load_native_config(native_config_path(tmp_path))
    assert saved.allowed_app_ids == ()
    assert saved.to_payload()["browser"] == payload["browser"]


def _setup_edit_host(tmp_path: Path) -> NativeDesktopHost:
    payload = _config_payload()
    payload.update(enabled=False, capture={"max_screenshot_width": 1200, "jpeg_quality": 65})
    payload["browser"] = {
        "enabled": True,
        "executable_path": str(tmp_path / "removed-browser"),
        "user_data_dir": str(tmp_path / "removed-profile"),
        "timeout_seconds": 45,
    }
    save_native_config(
        native_config_path(tmp_path),
        NativeDesktopConfig.from_payload(payload, validate_browser_paths=False),
        expected_revision=0,
    )
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret-token"),
    )
    return NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None), helper_version="1")


def _setup_edit_parameters(action: str) -> dict[str, object]:
    if action == "finish_setup":
        return {
            "expected_revision": 1,
            "expected_session": {
                "homeserver": "https://example.org",
                "user_id": "@me:example.org",
                "device_id": "LOCAL",
            },
        }
    return {"expected_revision": 1, "browser": {"enabled": False, "executable_path": None, "user_data_dir": None}}


def test_finish_setup_enables_only_reviewed_config_under_session_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = load_native_config(path).to_payload()
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    original_session = session_path.read_bytes()
    published = False

    def save_with_session_lock(
        path: Path,
        config: NativeDesktopConfig,
        *,
        expected_revision: int,
    ) -> NativeDesktopConfig:
        nonlocal published
        assert file_lock_is_held(session_path.with_suffix(".lock")), "Session replacement must be excluded during save"
        saved = save_native_config(path, config, expected_revision=expected_revision)
        assert file_lock_is_held(session_path.with_suffix(".lock")), "Keep the session locked through publication"
        published = True
        return saved

    monkeypatch.setattr("mindroom.desktop.native_host.save_native_config", save_with_session_lock)
    request = parse_native_request(
        json.dumps(
            {
                "v": 1,
                "request_id": str(uuid4()),
                "action": "finish_setup",
                "parameters": _setup_edit_parameters("finish_setup"),
            },
        ).encode(),
    )

    result = asyncio.run(host.handle(request))

    assert published
    assert load_native_config(path).to_payload() == original | {"revision": 2, "enabled": True}
    assert session_path.read_bytes() == original_session
    assert result["status"]["config"]["enabled"] is True


@pytest.mark.parametrize("field", ["homeserver", "user_id", "device_id"])
def test_finish_setup_rejects_changed_session_without_enabling(tmp_path: Path, field: str) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = path.read_bytes()
    parameters = _setup_edit_parameters("finish_setup")
    cast("dict[str, str]", parameters["expected_session"])[field] = "different"

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("finish_setup", **parameters)))

    assert caught.value.code == "session_conflict"
    assert path.read_bytes() == original


def test_finish_setup_rechecks_identity_after_waiting_for_session_replacement(tmp_path: Path) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = path.read_bytes()
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"

    async def replace_while_finishing() -> None:
        with advisory_file_lock(session_path.with_suffix(".lock")):
            finish = asyncio.create_task(
                host.handle(_request("finish_setup", **_setup_edit_parameters("finish_setup"))),
            )
            await asyncio.sleep(0)
            assert not finish.done(), "Finishing setup must wait for the in-progress session replacement"
            replacement = DesktopMatrixSession("https://example.org", "@me:example.org", "REPLACED", "new-token")
            session_path.write_text(json.dumps(replacement.to_payload()))
        with pytest.raises(NativeProtocolError) as caught:
            await asyncio.wait_for(finish, timeout=2)
        assert caught.value.code == "session_conflict"

    asyncio.run(replace_while_finishing())

    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_finish_setup_requires_valid_saved_session(tmp_path: Path, kind: str) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = path.read_bytes()
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    if kind == "missing":
        session_path.unlink()
    else:
        session_path.write_text("invalid session")

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("finish_setup", **_setup_edit_parameters("finish_setup"))))

    assert caught.value.code == "session_missing"
    assert path.read_bytes() == original


@pytest.mark.parametrize("identity", [None, {}, {"homeserver": "https://example.org"}, "LOCAL"])
def test_finish_setup_requires_exact_session_identity(tmp_path: Path, identity: object) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = path.read_bytes()

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("finish_setup", expected_revision=1, expected_session=identity)))

    assert caught.value.code == "invalid_request"
    assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", [None, "", "  ", 123])
def test_finish_setup_requires_nonempty_session_identity_fields(tmp_path: Path, invalid: object) -> None:
    host = _setup_edit_host(tmp_path)
    parameters = _setup_edit_parameters("finish_setup")
    cast("dict[str, object]", parameters["expected_session"])["device_id"] = invalid

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("finish_setup", **parameters)))

    assert caught.value.code == "invalid_request"
    assert load_native_config(native_config_path(tmp_path)).enabled is False


@pytest.mark.parametrize("action", ["finish_setup", "set_browser_config"])
def test_setup_edits_reject_stale_revision_without_mutating_config(tmp_path: Path, action: str) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    current = load_native_config(path)
    save_native_config(path, replace(current, allowed_app_ids=("com.example.Other",)), expected_revision=1)
    original = path.read_bytes()

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request(action, **_setup_edit_parameters(action))))

    assert caught.value.code == "revision_conflict"
    assert path.read_bytes() == original


def test_finish_setup_rejects_config_changed_during_session_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = load_native_config(path)

    def save_after_external_edit(
        path: Path,
        config: NativeDesktopConfig,
        *,
        expected_revision: int,
    ) -> NativeDesktopConfig:
        save_native_config(path, replace(original, allowed_app_ids=()), expected_revision=original.revision)
        return save_native_config(path, config, expected_revision=expected_revision)

    monkeypatch.setattr("mindroom.desktop.native_host.save_native_config", save_after_external_edit)

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("finish_setup", **_setup_edit_parameters("finish_setup"))))

    assert caught.value.code == "revision_conflict"
    assert load_native_config(path) == replace(original, revision=2, allowed_app_ids=())


def test_browser_save_preserves_disabled_setup_and_all_unedited_settings(tmp_path: Path) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = load_native_config(path).to_payload()
    executable = tmp_path / "browser"
    executable.touch()
    profile = tmp_path / "profile"
    profile.mkdir()
    browser = {"enabled": True, "executable_path": str(executable), "user_data_dir": str(profile)}
    request = parse_native_request(
        json.dumps(
            {
                "v": 1,
                "request_id": str(uuid4()),
                "action": "set_browser_config",
                "parameters": {"expected_revision": 1, "browser": browser},
            },
        ).encode(),
    )

    result = asyncio.run(host.handle(request))

    assert load_native_config(path).to_payload() == original | {
        "revision": 2,
        "browser": browser | {"timeout_seconds": 45},
    }
    assert result["status"]["config"]["enabled"] is False


def test_browser_save_allows_disabling_unchanged_missing_paths(tmp_path: Path) -> None:
    host = _setup_edit_host(tmp_path)
    config = load_native_config(native_config_path(tmp_path))
    browser = cast("dict[str, object]", config.to_payload()["browser"])
    browser.pop("timeout_seconds")
    browser["enabled"] = False

    asyncio.run(host.handle(_request("set_browser_config", expected_revision=1, browser=browser)))

    assert load_native_config(native_config_path(tmp_path)) == replace(
        config,
        revision=2,
        browser=replace(config.browser, enabled=False),
    )


@pytest.mark.parametrize("field", ["executable_path", "user_data_dir"])
@pytest.mark.parametrize("invalid", ["missing", "wrong_kind", "relative"])
def test_browser_save_validates_changed_paths(tmp_path: Path, field: str, invalid: str) -> None:
    host = _setup_edit_host(tmp_path)
    path = native_config_path(tmp_path)
    original = path.read_bytes()
    changed = tmp_path / "changed"
    if invalid == "wrong_kind":
        if field == "executable_path":
            changed.mkdir()
        else:
            changed.touch()
    browser = {"enabled": False, "executable_path": None, "user_data_dir": None}
    browser[field] = "relative" if invalid == "relative" else str(changed)

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("set_browser_config", expected_revision=1, browser=browser)))

    assert caught.value.code == "invalid_request"
    assert path.read_bytes() == original


@pytest.mark.parametrize("action", ["finish_setup", "set_browser_config"])
def test_setup_edits_require_saved_configuration_and_stopped_runtime(tmp_path: Path, action: str) -> None:
    host = NativeDesktopHost(SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None), helper_version="1")
    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request(action, **_setup_edit_parameters(action))))
    assert caught.value.code == "configuration_missing"
    host = _setup_edit_host(tmp_path)
    original = native_config_path(tmp_path).read_bytes()
    host._runtime = FakeRuntime()

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request(action, **_setup_edit_parameters(action))))

    assert caught.value.code == "busy"
    assert native_config_path(tmp_path).read_bytes() == original


@pytest.mark.parametrize("journal_kind", ["malformed", "directory", "symlink"])
def test_configure_rejects_unreadable_journal_without_creating_configuration(tmp_path: Path, journal_kind: str) -> None:
    journal_path = tmp_path / "desktop_bridge" / "commands.sqlite3"
    journal_path.parent.mkdir()
    if journal_kind == "directory":
        journal_path.mkdir(mode=0o700)
    elif journal_kind == "symlink":
        journal_path.symlink_to(tmp_path / "missing.sqlite3")
    else:
        journal_path.write_bytes(b"broken journal")
        journal_path.chmod(0o600)
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
    )

    with pytest.raises(NativeProtocolError) as caught:
        asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))

    assert caught.value.code == "invalid_request"
    assert not (journal_path.parent / "native_config.json").exists()
    if journal_kind == "malformed":
        assert journal_path.read_bytes() == b"broken journal"
    if journal_kind == "symlink":
        assert journal_path.is_symlink()
        assert not (tmp_path / "missing.sqlite3").exists()


@pytest.mark.parametrize("kind", ["malformed", "exposed"])
def test_configure_repairs_supported_files_using_reported_revision(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "desktop_bridge" / "native_config.json"
    path.parent.mkdir()
    revision = 4 if kind == "exposed" else 0
    payload = _config_payload()
    payload["revision"] = revision
    path.write_text(json.dumps(payload) if kind == "exposed" else "{broken json")
    path.chmod(0o644 if kind == "exposed" else 0o600)
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
    )
    status = host.status()
    assert status["config"]["state"] == "invalid"
    assert status["config"]["revision"] == revision

    result = asyncio.run(host.handle(_request("configure", expected_revision=revision, config=payload)))

    assert result["status"]["config"]["state"] == "ready"
    assert result["status"]["config"]["revision"] == revision + 1
    assert json.loads(path.read_text())["revision"] == revision + 1


def test_host_login_and_pair_never_echo_secrets(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def login(_paths: object, parameters: dict[str, object]) -> dict[str, object]:
        calls.append(("login", parameters))
        return {"homeserver": "https://example.org", "user_id": "@me:example.org", "device_id": "LOCAL"}

    async def pair(_paths: object, config: NativeDesktopConfig, parameters: dict[str, object]) -> dict[str, object]:
        calls.append(("pair", parameters))
        assert config.controller.device_id == "DEVICE"
        return {"verification": "ABCD-EFGH", "confirmation_command": "!desktop confirm code ABCD-EFGH"}

    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(
            runtime_factory=lambda _paths, _config: FakeRuntime(),
            login=login,
            pair=pair,
        ),
    )
    asyncio.run(host.handle(_request("configure", expected_revision=0, config=_config_payload())))
    login_result = asyncio.run(
        host.handle(
            _request(
                "login",
                homeserver="https://example.org",
                user_id="@me:example.org",
                method="password",
                password="secret-value",
            ),
        ),
    )
    pair_result = asyncio.run(host.handle(_request("pair", code="code")))
    rendered = repr((login_result, pair_result, host.status()))
    assert "secret-value" not in rendered
    assert pair_result["confirmation_command"].startswith("!desktop confirm")
    assert [name for name, _ in calls] == ["login", "pair"]


def test_import_setup_validates_and_keeps_pairing_code_transient(tmp_path: Path) -> None:
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: FakeRuntime()),
    )
    descriptor = {
        "v": 1,
        "kind": "mindroom_desktop_setup",
        "homeserver": "https://example.org",
        "user_id": "@local:example.org",
        "code": "one-time-code",
        "controller_user_id": "@controller:example.org",
        "controller_device_id": "DEVICE",
        "controller_ed25519": "key",
        "requester_id": "@person:example.org",
        "agent_name": "assistant",
        "cloudflare_access": False,
    }
    result = asyncio.run(host.handle(_request("import_setup", descriptor=descriptor)))
    assert result == descriptor
    assert "one-time-code" not in repr(host.status())
    assert not (tmp_path / "desktop_bridge" / "native_config.json").exists()


def test_stream_emits_correlated_error_and_stops_on_eof(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
    )
    input_stream = io.BytesIO(
        ('{"v":1,"request_id":"' + str(uuid4()) + '","action":"start","parameters":{}}\n').encode(),
    )
    output_stream = io.BytesIO()
    asyncio.run(serve_native_stream(host, input_stream=input_stream, output_stream=output_stream))
    messages = [line for line in output_stream.getvalue().decode().splitlines()]
    assert '"type":"hello"' in messages[0]
    assert any('"code":"configuration_missing"' in line for line in messages)
    assert host.status()["authority"]["control_available"] is False


def test_stream_drains_one_oversized_record_before_next_request(tmp_path: Path) -> None:
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
        dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: FakeRuntime()),
    )
    request_id = str(uuid4())
    input_stream = io.BytesIO(
        b"{"
        + b"x" * 70_000
        + b"}\n"
        + ('{"v":1,"request_id":"' + request_id + '","action":"status","parameters":{}}\n').encode(),
    )
    output_stream = io.BytesIO()
    asyncio.run(serve_native_stream(host, input_stream=input_stream, output_stream=output_stream))
    output = output_stream.getvalue().decode()
    assert output.count('"code":"request_too_large"') == 1
    assert f'"request_id":"{request_id}"' in output


@pytest.mark.parametrize("immediate_action", ["revoke_control", "decide_shell", "revoke_shell"])
def test_stream_caps_regular_requests_and_preserves_revoke_lane(immediate_action: str) -> None:
    class SaturatedHost:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.active = 0
            self.maximum_active = 0
            self.revoked = False

        def hello(self) -> dict[str, object]:
            return {"v": 1, "type": "hello"}

        def status(self) -> dict[str, object]:
            return {"revoked": self.revoked}

        async def handle(self, request: NativeRequest) -> dict[str, object]:
            if request.action == immediate_action:
                self.revoked = True
                self.release.set()
                return {"status": self.status()}
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await self.release.wait()
                return {}
            finally:
                self.active -= 1

        async def shutdown(self) -> None:
            self.release.set()

    def record(action: str) -> bytes:
        return json.dumps({"v": 1, "request_id": str(uuid4()), "action": action, "parameters": {}}).encode() + b"\n"

    host = SaturatedHost()
    input_stream = io.BytesIO(b"".join([record("login") for _ in range(5)] + [record(immediate_action)]))
    output_stream = io.BytesIO()
    asyncio.run(serve_native_stream(host, input_stream=input_stream, output_stream=output_stream))  # type: ignore[arg-type]
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]

    assert host.maximum_active == 4
    assert host.revoked is True
    assert sum(message.get("error", {}).get("code") == "busy" for message in messages) == 1


def test_eof_waits_for_inflight_stop_and_retains_runtime_ownership(tmp_path: Path) -> None:
    class SlowStopRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()
            self.stop_cancelled = False
            self.stop_drained = False

        async def stop(self) -> None:
            self.stop_started.set()
            try:
                await self.release_stop.wait()
            except asyncio.CancelledError:
                self.stop_cancelled = True
                raise
            self.stop_drained = True
            await super().stop()

    async def scenario() -> tuple[bool, bool, bool, bool, str]:
        runtime = SlowStopRuntime()
        host = NativeDesktopHost(
            SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
            helper_version="1",
            dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
        )
        await host.handle(_request("configure", expected_revision=0, config=_config_payload()))
        await host.handle(_request("start"))
        input_stream = io.BytesIO(
            json.dumps({"v": 1, "request_id": str(uuid4()), "action": "stop", "parameters": {}}).encode() + b"\n",
        )
        stream = asyncio.create_task(serve_native_stream(host, input_stream=input_stream, output_stream=io.BytesIO()))
        await runtime.stop_started.wait()
        await asyncio.sleep(0)
        pending_at_eof = not stream.done()
        ownership_retained = host._runtime is runtime
        bridge_state = str(host.status()["bridge"]["state"])
        runtime.release_stop.set()
        await stream
        return pending_at_eof, ownership_retained, runtime.stop_cancelled, runtime.stop_drained, bridge_state

    assert asyncio.run(scenario()) == (True, True, False, True, "stopping")


def test_transport_failure_fences_queued_control_before_worker_is_cancelled() -> None:
    transport_failed = asyncio.Event()
    worker_released = asyncio.Event()
    accepting = True
    executed = False

    async def transport() -> None:
        await transport_failed.wait()
        raise RuntimeError("transport failed")

    async def worker() -> None:
        nonlocal executed
        await worker_released.wait()
        if accepting:
            executed = True

    async def fence() -> None:
        nonlocal accepting
        accepting = False
        worker_released.set()
        await asyncio.sleep(0)

    async def scenario() -> BaseException:
        tasks = {asyncio.create_task(worker()), asyncio.create_task(transport())}
        transport_failed.set()
        return await supervise_native_tasks(tasks, fence)

    failure = asyncio.run(scenario())
    assert str(failure) == "transport failed"
    assert executed is False


def test_revoke_bypasses_slow_lifecycle_operation(tmp_path: Path) -> None:
    async def scenario() -> int:
        runtime = FakeRuntime()
        host = NativeDesktopHost(
            SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
            helper_version="1",
            dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
        )
        await host.handle(_request("configure", expected_revision=0, config=_config_payload()))
        await host.handle(_request("start"))
        await host._lock.acquire()
        try:
            await asyncio.wait_for(host.handle(_request("revoke_control")), timeout=0.1)
        finally:
            host._lock.release()
        await host.shutdown()
        return runtime.revoked

    assert asyncio.run(scenario()) == 1


def test_host_remains_available_when_config_path_is_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "desktop_bridge" / "native_config.json"
    path.mkdir(parents=True, mode=0o700)
    host = NativeDesktopHost(
        SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
        helper_version="1",
    )
    status = host.status()
    assert status["config"]["state"] == "invalid"
    assert status["bridge"]["last_error"]["code"] == "invalid_request"


def test_stop_cancels_pending_start_before_it_can_publish_running(tmp_path: Path) -> None:
    class SlowStartRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self) -> None:
            self.entered.set()
            await self.release.wait()
            await super().start()

    async def scenario() -> None:
        runtime = SlowStartRuntime()
        host = NativeDesktopHost(
            SimpleNamespace(storage_root=tmp_path, env_value=lambda *_args: None),
            helper_version="1",
            dependencies=NativeHostDependencies(runtime_factory=lambda _paths, _config: runtime),
        )
        await host.handle(_request("configure", expected_revision=0, config=_config_payload()))
        starting = asyncio.create_task(host.handle(_request("start")))
        await runtime.entered.wait()
        stopping = asyncio.create_task(host.handle(_request("stop")))
        await asyncio.sleep(0)
        runtime.release.set()
        stopped, start_result = await asyncio.gather(stopping, starting, return_exceptions=True)
        assert isinstance(stopped, dict)
        assert stopped["status"]["bridge"]["state"] == "stopped"
        assert isinstance(start_result, NativeProtocolError)
        assert start_result.code == "not_running"
        assert not runtime.running
        # A cancelled start must release ownership for a subsequent explicit start.
        await host.handle(_request("start"))
        assert runtime.running
        await host.shutdown()

    asyncio.run(scenario())
