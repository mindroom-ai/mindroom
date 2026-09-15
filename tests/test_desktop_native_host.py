"""Native desktop host lifecycle tests."""

# Compact fakes keep the wire-level lifecycle assertions readable.
# ruff: noqa: C416, D101, D102, D103, EM101, S106, TC001, TC003, TRY003

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from mindroom.desktop.command_journal import DesktopCommandJournal
from mindroom.desktop.native_config import NativeDesktopConfig
from mindroom.desktop.native_host import (
    NativeDesktopHost,
    NativeHostDependencies,
    serve_native_stream,
    supervise_native_tasks,
)
from mindroom.desktop.native_protocol import NativeProtocolError, NativeRequest
from mindroom.desktop.protocol import DesktopCommand


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

    async def connect_browser(self) -> None:
        pass

    async def disconnect_browser(self) -> None:
        pass


def _request(action: str, **parameters: object) -> NativeRequest:
    return NativeRequest(str(uuid4()), action, parameters)


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


def test_stream_caps_regular_requests_and_preserves_revoke_lane() -> None:
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
            if request.action == "revoke_control":
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
    input_stream = io.BytesIO(b"".join([record("login") for _ in range(5)] + [record("revoke_control")]))
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
