"""Tests for local desktop policy and accessibility-first execution."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import threading
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nio
import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent

from mindroom.desktop.accessibility import (
    DesktopApp,
    MacAccessibilityBackend,
)
from mindroom.desktop.bridge import (
    DesktopBridge,
    DesktopBridgePolicy,
    _DesktopBridgeStoppedError,
    _run_macos_application_events,
)
from mindroom.desktop.command_journal import DesktopCommandJournalError
from mindroom.desktop.filesystem import DesktopFilesystem
from mindroom.desktop.media import DesktopMediaError, download_encrypted_media
from mindroom.desktop.playwright_mcp import (
    BrowserImage,
    BrowserProviderResult,
    PlaywrightActionOutcomeUnknownError,
)
from mindroom.desktop.protocol import (
    DESKTOP_COMMAND_EVENT_TYPE,
    MAX_INLINE_RESPONSE_BYTES,
    DesktopCommand,
    DesktopResponse,
    EncryptedDesktopMedia,
)
from mindroom.desktop.reply_fitting import _WIDEST_METRICS
from mindroom.desktop.shell import DesktopShell, DesktopShellError
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from tests.desktop_helpers import (
    _LARGE_OUTPUT,
    _LONGEST_SESSION_ID,
    _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    ALICE,
    APP_ID,
    BOB,
    MEDIA,
    NOW_SECONDS,
    PRIVATE_COMMAND,
    STATE,
    WINDOW,
    FakeProvider,
    _assert_upload_fallback,
    _command,
    _handle_command,
    _local_shell,
    _longest_request_id,
    _root_id,
    _run_shell,
    _stall_output_uploads,
)
from tests.desktop_helpers import selected_root as selected_root  # noqa: PLC0414
from tests.test_olm_to_device import olm_transport

# The Matrix spec sets no to-device or EDU size limit (matrix-org/matrix-doc#3121). Synapse 1.148 caps
# request bodies at 200 * 65,536 bytes and Tuwunel at 24 MiB, and one federation transaction carries up to
# 50 PDUs and 100 EDUs within that cap. Each encrypted to-device request therefore stays within the
# 65,536-byte event limit, so a full transaction of them still fits. Only this test checks it: production
# never reads this value, so it lives here rather than in mindroom.desktop.protocol.
_MAX_TO_DEVICE_BYTES = 65_536


CONTROLLER = PinnedMatrixDevice("@cloud:example.org", "CLOUD", "cloud-fingerprint")


@dataclass
class FakeBrowserProvider:
    """Record browser actions handled inside the local Matrix bridge."""

    result: BrowserProviderResult = field(
        default_factory=lambda: BrowserProviderResult(
            {"action": "snapshot", "provider": "playwright_mcp_extension", "result": "snapshot", "status": "ok"},
        ),
    )
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    error: Exception | None = None

    async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
        """Record and return the planned result."""
        self.calls.append((action, parameters))
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        """Satisfy the provider lifecycle contract."""


def _event(command: DesktopCommand) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": command.to_content()},
        sender=CONTROLLER.user_id,
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice(
            CONTROLLER.user_id,
            CONTROLLER.device_id,
            "controller-curve-key",
            CONTROLLER.ed25519,
        ),
    )


def _policy(*, allow_control: bool = False, browser_enabled: bool = False) -> DesktopBridgePolicy:
    return DesktopBridgePolicy(
        controller=CONTROLLER,
        allowed_requester_ids=frozenset({"@alice:example.org"}),
        allowed_agent_names=frozenset({"computer"}),
        allowed_app_ids=frozenset({APP_ID}),
        allow_control=allow_control,
        control_lease_expires_at_ms=20_000 if allow_control else None,
        browser_enabled=browser_enabled,
    )


def _response(send: AsyncMock) -> DesktopResponse:
    content = send.await_args.kwargs["content"]
    return DesktopResponse.from_content(content)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Accept the exact controller identity while capturing encrypted responses."""
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.bridge.resolve_pinned_device", AsyncMock())
    upload = AsyncMock(return_value=MEDIA)
    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload)
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload)
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", send)
    return send


async def _execute(bridge: DesktopBridge) -> None:
    """Drain both executor lanes once, as the bridge's two worker loops would."""
    await asyncio.gather(bridge.execute_pending(shell_starts=False), bridge.execute_pending(shell_starts=True))


async def _handle(bridge: DesktopBridge, event: AuthenticatedToDeviceEvent) -> None:
    """Drive stages with the preobserved state assumed by the fake OS provider."""
    command = DesktopCommand.from_content(event.source["content"])
    state_id = command.parameters.get("state_id")
    if isinstance(state_id, str):
        bridge._observations.remember(replace(STATE, state_id=state_id), command)
    await bridge.on_to_device_event(event)
    await _execute(bridge)
    await bridge.deliver_pending()


def _local_bridge(
    *,
    filesystem: DesktopFilesystem | None = None,
    shell: DesktopShell | None = None,
    journal_path: Path | None = None,
) -> DesktopBridge:
    """Build a bridge with only folder and shell capabilities and no GUI provider."""
    roots = tuple(Path(str(folder["path"])) for folder in filesystem.list_folders()["folders"]) if filesystem else ()
    policy = replace(
        _policy(),
        allowed_requester_ids=frozenset({ALICE, BOB}),
        allowed_app_ids=frozenset(),
        allowed_file_roots=roots,
        shell_enabled=shell is not None,
    )
    return DesktopBridge(
        client=object(),
        provider=None,
        policy=policy,
        filesystem=filesystem,
        shell=shell,
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )


async def _wait_for_pending_shell(bridge: DesktopBridge) -> dict[str, object]:
    for _ in range(200):
        pending = bridge.local_status()["shell"]["pending"]
        if pending is not None:
            return pending
        await asyncio.sleep(0.005)
    pytest.fail("shell approval never became pending")


@pytest.mark.asyncio
async def test_file_only_bridge_lists_and_reads_selected_folder_without_gui(
    transport: AsyncMock,
    selected_root: Path,
) -> None:
    """Folder actions work through the remote channel without any GUI provider or app selection."""
    files = DesktopFilesystem((selected_root,))
    bridge = _local_bridge(filesystem=files)
    root_id = _root_id(files)

    await _handle(bridge, _event(_command("list_folders")))
    assert _response(transport).result["folders"] == [{"id": root_id, "name": "selected", "path": str(selected_root)}]
    await _handle(bridge, _event(_command("status", request_id="r2", sequence=2)))
    status = _response(transport).result
    assert status["gui_available"] is False
    assert status["bridge"]["gui_available"] is False
    assert status["bridge"]["file_roots"] == [{"id": root_id, "name": "selected", "path": str(selected_root)}]
    assert status["bridge"]["shell"] == {
        "enabled": False,
        "pending": False,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert type(status["bridge"]["shell"]["auto_approve_remaining_seconds"]) is int
    await _handle(bridge, _event(_command("list_apps", request_id="r3", sequence=3)))
    assert _response(transport).result == {"apps": [], "metrics": _response(transport).result["metrics"]}
    await _handle(bridge, _event(_command("get_app_state", request_id="r4", sequence=4)))
    assert _response(transport).error == "Desktop command must target an application in the local allowlist."
    bridge.close()


@pytest.mark.asyncio
async def test_remote_status_file_roots_is_bounded_while_local_status_keeps_every_root(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """The remote status reply's file_roots must fit the envelope; the local view is never trimmed."""
    base = tmp_path / ("a" * 200) / ("b" * 200)
    base.mkdir(parents=True)
    roots = []
    for number in range(80):
        leaf = base / f"root-{number:03}-{'c' * 200}"
        leaf.mkdir()
        roots.append(leaf)
    files = DesktopFilesystem(tuple(roots))
    bridge = _local_bridge(filesystem=files)
    command = _command(
        "status",
        request_id="r" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        session_id="s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    )
    await _handle(bridge, _event(command))
    response = _response(transport)
    assert response.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    file_roots = response.result["bridge"]["file_roots"]
    assert isinstance(file_roots, list)
    assert response.result["bridge"]["file_roots_truncated"] is True
    assert 0 < len(file_roots) < len(roots)
    # The native NDJSON channel the Mac app reads has no to-device limit, so it keeps every root.
    assert len(bridge.local_status()["file_roots"]) == len(roots)
    bridge.close()


@pytest.mark.asyncio
async def test_truncated_read_file_reply_is_recorded_as_sent_within_the_budget(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """A read trimmed to the inline budget is sent exactly as recorded, metrics included, within one reply."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    content = ("é" * 20_000 + "€" * 5_000 + "\U0001f600" * 3_000 + '"\\\t\n' * 2_000).encode()
    (root / "notes.txt").write_bytes(content)
    files = DesktopFilesystem((root,))
    bridge = _local_bridge(filesystem=files)
    request_id = _longest_request_id("read-")
    command = _command(
        "read_file",
        request_id=request_id,
        session_id=_LONGEST_SESSION_ID,
        parameters={"root_id": _root_id(files), "path": "notes.txt"},
    )
    await _handle(bridge, _event(command))
    sent = _response(transport)
    assert sent.to_content() == bridge._journal.get(request_id).response.to_content()
    assert "metrics" in sent.result
    assert sent.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    assert sent.result["truncated"] is True
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("requester_id", "agent_name"), [("@eve:example.org", "computer"), (ALICE, "other")])
async def test_denied_callers_get_no_file_bytes_or_shell_process(
    transport: AsyncMock,
    selected_root: Path,
    tmp_path: Path,
    requester_id: str,
    agent_name: str,
) -> None:
    """Caller policy rejects file and shell work before either local provider runs."""
    files = DesktopFilesystem((selected_root,))
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(filesystem=files, shell=shell)
    caller = {"requester_id": requester_id, "agent_name": agent_name}
    await _handle(
        bridge,
        _event(_command("read_file", parameters={"root_id": _root_id(files), "path": "note.txt"}, **caller)),
    )
    denied_read = _response(transport)
    await _handle(
        bridge,
        _event(
            _command(
                "run_shell",
                request_id="r2",
                sequence=2,
                parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)},
                **caller,
            ),
        ),
    )
    denied_shell = _response(transport)
    for response in (denied_read, denied_shell):
        assert response.error == "Desktop command requester or agent is not allowed by local policy."
        assert response.result == {}
    assert "private text" not in json.dumps(denied_read.to_content())
    assert not (tmp_path / "marker").exists()
    assert bridge.local_status()["shell"]["active_request_id"] is None
    bridge.close()


@pytest.mark.asyncio
async def test_disabled_local_capability_is_rejected_before_provider(
    transport: AsyncMock,
    selected_root: Path,
    tmp_path: Path,
) -> None:
    """A folder-only bridge has no shell, and a shell-only bridge has no folder reads."""
    files_only = _local_bridge(filesystem=DesktopFilesystem((selected_root,)))
    await _handle(files_only, _event(_command("run_shell", parameters={"command": PRIVATE_COMMAND})))
    assert _response(transport).error == "Local shell access is disabled."
    files_only.close()
    shell_only = _local_bridge(shell=_local_shell())
    await _handle(shell_only, _event(_command("list_folders")))
    assert _response(transport).error == "Local file access is disabled."
    assert shell_only.local_status()["shell"]["pending"] is None
    shell_only.close()
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("list_folders", {"observation": "both"}),
        ("run_shell", {"command": PRIVATE_COMMAND, "observation": "both"}),
    ],
)
async def test_local_actions_reject_the_observation_parameter(
    transport: AsyncMock,
    selected_root: Path,
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
) -> None:
    """Folder and shell actions take no observation mode; the bridge rejects one before any read or approval."""
    files = DesktopFilesystem((selected_root,))
    shell = _local_shell()
    bridge = _local_bridge(filesystem=files, shell=shell)
    if action == "run_shell":
        parameters = {**parameters, "cwd": str(tmp_path)}
    await _handle(bridge, _event(_command(action, parameters=parameters)))
    assert _response(transport).error == "Unexpected desktop parameters: observation."
    assert shell.status()["pending"] is None
    assert not (tmp_path / "marker").exists()
    bridge.close()


@pytest.mark.asyncio
async def test_exact_local_approval_runs_shell_once_and_restart_never_replays(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """Only the exact pending ID starts the process, and redelivery returns the saved receipt."""
    journal = tmp_path / "commands.sqlite3"
    bridge = _local_bridge(shell=_local_shell(), journal_path=journal)
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    await _wait_for_pending_shell(bridge)
    with pytest.raises(DesktopShellError):
        bridge.decide_local_shell("another-request", approved=True, auto_approve_seconds=0)
    assert not (tmp_path / "marker").exists()
    bridge.decide_local_shell("request-1", approved=True, auto_approve_seconds=0)
    await execution
    await bridge.deliver_pending()
    completed = _response(transport)
    assert completed.result["exit_code"] == 0
    assert (tmp_path / "marker").read_text() == "private-shell-text"
    with pytest.raises(DesktopShellError):
        bridge.decide_local_shell("request-1", approved=True, auto_approve_seconds=0)
    await _handle(bridge, _event(command))
    assert _response(transport).to_content() == completed.to_content()
    await bridge.stop()
    bridge.close()

    restarted = _local_bridge(shell=_local_shell(), journal_path=journal)
    restarted.recover_interrupted()
    await _handle(restarted, _event(command))
    assert _response(transport).to_content() == completed.to_content()
    assert restarted.local_status()["shell"]["pending"] is None
    assert (tmp_path / "marker").read_text() == "private-shell-text"
    restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pending", "running"])
async def test_stop_settles_shell_work_promptly_without_replay(
    transport: AsyncMock,
    tmp_path: Path,
    phase: str,
) -> None:
    """Stop closes the shell before draining, so neither approval nor a process holds it open."""
    journal = tmp_path / "commands.sqlite3"
    started = tmp_path / "started"
    expected_runs = "started" if phase == "running" else None
    shell = _local_shell()
    if phase == "running":
        shell.grant(60)
    bridge = _local_bridge(shell=shell, journal_path=journal)
    command = _command("run_shell", parameters={"command": "printf started >> started; sleep 30", "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    if phase == "pending":
        await _wait_for_pending_shell(bridge)
    else:
        # Wait for the marker's content, not just its creation: `>>` opens (and so creates) the
        # file before the write lands, so `exists()` alone can observe it empty.
        for _ in range(200):
            if started.exists() and started.read_text() == expected_runs:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("shell command never finished writing its started marker")
    assert (started.read_text() if started.exists() else None) == expected_runs
    await asyncio.wait_for(bridge.stop(), timeout=3)
    await execution
    # Native shutdown closes the transport as soon as stop returns. No caller-side flush.
    assert transport.await_args is not None, "Stop must deliver the cancellation before closing transport"
    stopped = _response(transport)
    assert stopped.error is not None
    assert ("did not run" if phase == "pending" else "was stopped") in stopped.error
    bridge.close()

    restarted = _local_bridge(shell=_local_shell(), journal_path=journal)
    restarted.recover_interrupted()
    await _handle(restarted, _event(command))
    assert _response(transport).to_content() == stopped.to_content()
    assert restarted.local_status()["shell"]["pending"] is None
    assert (started.read_text() if started.exists() else None) == expected_runs
    restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["offline", "unexpected", "stalled", "sender_busy"])
async def test_stop_preserves_undelivered_cancellation_when_delivery_fails(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Stop stays bounded even if delivery is already blocked, and restart delivers without executing."""
    monkeypatch.setattr("mindroom.desktop.bridge._STOP_DELIVERY_TIMEOUT_SECONDS", 0.05)
    journal = tmp_path / "commands.sqlite3"
    bridge = _local_bridge(shell=_local_shell(), journal_path=journal)
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    await _wait_for_pending_shell(bridge)
    sending = asyncio.Event()

    async def failed_send(*_args: object, **_kwargs: object) -> None:
        sending.set()
        if failure == "offline":
            message = "Offline"
            raise OlmToDeviceError(message)
        if failure == "unexpected":
            message = "Unexpected encrypted delivery failure"
            raise RuntimeError(message)
        await asyncio.Event().wait()

    transport.side_effect = failed_send
    sender = None
    if failure == "sender_busy":
        await bridge.on_to_device_event(_event(_command("status", request_id="status", sequence=2)))
        await bridge.execute_pending(shell_starts=False)
        sender = asyncio.create_task(bridge.deliver_pending())
        await asyncio.wait_for(sending.wait(), 1)
    try:
        await asyncio.wait_for(bridge.stop(), 1)
        await execution
        assert sending.is_set(), "Shutdown must attempt delivery"
        assert not (tmp_path / "marker").exists()
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        bridge.close()

    transport.side_effect = None
    transport.reset_mock()
    restarted = _local_bridge(shell=_local_shell(), journal_path=journal)
    try:
        restarted.recover_interrupted()
        await restarted.deliver_pending()
        replies = [DesktopResponse.from_content(call.kwargs["content"]) for call in transport.await_args_list]
        cancellation = next(reply for reply in replies if reply.request_id == command.request_id)
        assert "did not run" in (cancellation.error or "")
        assert not (tmp_path / "marker").exists()
        assert not restarted._journal.pending_responses()
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_other_callers_see_shell_state_without_pending_command(transport: AsyncMock, tmp_path: Path) -> None:
    """Remote status and receipts expose no pending command text to another allowed caller."""
    shell = _local_shell()
    bridge = _local_bridge(shell=shell)
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    await _wait_for_pending_shell(bridge)
    receipt = _command(
        "request_status",
        request_id="query",
        sequence=2,
        requester_id=BOB,
        parameters={"request_id": "request-1"},
    )
    await bridge.on_to_device_event(_event(receipt))
    await bridge.deliver_pending()
    assert _response(transport).result == {"request_id": "request-1", "state": "not_found"}
    # Shell starts wait for approval in their own lane, so status still runs while this request is pending.
    await _handle(bridge, _event(_command("status", request_id="status", sequence=3, requester_id=BOB)))
    status = _response(transport)
    assert status.result["bridge"]["shell"] == {
        "enabled": True,
        "pending": True,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert "private-shell-text" not in json.dumps(status.to_content())
    assert bridge.local_status()["shell"]["pending"]["command"] == PRIVATE_COMMAND
    bridge.decide_local_shell("request-1", approved=False, auto_approve_seconds=0)
    await execution
    assert not (tmp_path / "marker").exists()
    bridge.close()


_APP_INPUTS = [
    ("click", {"x": 100, "y": 200, "button": "left"}),
    ("double_click", {"x": 100, "y": 200, "button": "left"}),
    ("hover", {"x": 100, "y": 200}),
    ("drag", {"start_x": 100, "start_y": 200, "end_x": 400, "end_y": 500, "duration_ms": 500}),
    ("scroll", {"direction": "down", "pages": 1}),
    ("type_text", {"text": "a"}),
    ("keypress", {"keys": ["enter"]}),
    ("click_element", {"element_index": 0}),
    ("set_value", {"element_index": 0, "value": "a"}),
    ("scroll_element", {"element_index": 0, "direction": "down", "pages": 1}),
    ("perform_action", {"element_index": 0, "action_name": "AXPress"}),
]


def _control_shell_bridge(provider: FakeProvider, shell: DesktopShell) -> DesktopBridge:
    """Build a bridge with a live control lease and shell requests, as a Mac with both enabled runs."""
    return DesktopBridge(
        client=object(),
        provider=provider,
        policy=replace(_policy(allow_control=True), shell_enabled=True),
        shell=shell,
        clock=lambda: NOW_SECONDS,
    )


def _app_input(action: str, parameters: dict[str, object], *, sequence: int) -> DesktopCommand:
    return _command(
        action,
        request_id=f"{action}-{sequence}",
        sequence=sequence,
        parameters={"app": APP_ID, "state_id": "state-1", **parameters},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "parameters"), _APP_INPUTS)
async def test_app_input_is_refused_while_a_shell_command_awaits_approval(
    transport: AsyncMock,
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
) -> None:
    """Agent input could press the approval card's buttons or answer the terminal prompt, so it waits its turn."""
    provider = FakeProvider()
    bridge = _control_shell_bridge(provider, _local_shell())
    await bridge.on_to_device_event(_event(_run_shell(PRIVATE_COMMAND, tmp_path, expires_at_ms=120_000)))
    shell_lane = asyncio.create_task(bridge.execute_pending(shell_starts=True))
    await _wait_for_pending_shell(bridge)
    await _handle(bridge, _event(_app_input(action, parameters, sequence=2)))
    refused = _response(transport)
    assert not refused.ok
    assert "waiting for local approval" in str(refused.error)
    assert provider.calls == []
    bridge.decide_local_shell("run", approved=False, auto_approve_seconds=0)
    await shell_lane
    await _handle(bridge, _event(_app_input(action, parameters, sequence=3)))
    assert _response(transport).ok
    assert provider.calls[0][0] == action
    assert not (tmp_path / "marker").exists()
    bridge.close()


@dataclass
class _BlockingClickProvider(FakeProvider):
    """Hold one click inside the provider until the test releases it."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Block like slow activation and input posting do on a real Mac."""
        self.started.set()
        assert self.release.wait(5)
        super().click(app_id=app_id, state_id=state_id, x=x, y=y, button=button)


@pytest.mark.asyncio
async def test_launch_app_still_works_while_a_shell_command_awaits_approval(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """Launching or foregrounding an allowed app sends it no input, so it is not paused."""
    provider = FakeProvider()
    bridge = _control_shell_bridge(provider, _local_shell())
    await bridge.on_to_device_event(_event(_run_shell(PRIVATE_COMMAND, tmp_path, expires_at_ms=120_000)))
    shell_lane = asyncio.create_task(bridge.execute_pending(shell_starts=True))
    await _wait_for_pending_shell(bridge)
    await _handle(bridge, _event(_command("launch_app", request_id="launch", sequence=2)))
    assert _response(transport).ok
    assert provider.calls[0] == ("launch_app", APP_ID)
    bridge.decide_local_shell("run", approved=False, auto_approve_seconds=0)
    await shell_lane
    bridge.close()


@pytest.mark.asyncio
async def test_shell_approval_is_shown_only_after_app_input_in_progress_finishes(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request arriving mid-click waits, so the click cannot land on a card that appeared under it."""
    monkeypatch.setattr("mindroom.desktop.shell._AGENT_INPUT_SETTLE_SECONDS", 0.01)
    provider = _BlockingClickProvider()
    bridge = _control_shell_bridge(provider, _local_shell())
    click = _app_input("click", {"x": 100, "y": 200, "button": "left"}, sequence=1)
    bridge._observations.remember(replace(STATE, state_id="state-1"), click)
    await bridge.on_to_device_event(_event(click))
    input_lane = asyncio.create_task(bridge.execute_pending(shell_starts=False))
    try:
        assert await asyncio.to_thread(provider.started.wait, 5)
        run = _run_shell(PRIVATE_COMMAND, tmp_path, sequence=2, expires_at_ms=120_000)
        await bridge.on_to_device_event(_event(run))
        shell_lane = asyncio.create_task(bridge.execute_pending(shell_starts=True))
        for _ in range(20):
            await asyncio.sleep(0.01)
            assert bridge.local_status()["shell"]["pending"] is None
    finally:
        provider.release.set()
    await input_lane
    assert (await _wait_for_pending_shell(bridge))["request_id"] == "run"
    bridge.decide_local_shell("run", approved=False, auto_approve_seconds=0)
    await shell_lane
    await bridge.deliver_pending()
    assert _response(transport).error == "Shell command denied locally."
    assert provider.calls[0][0] == "click"
    bridge.close()


@pytest.mark.asyncio
async def test_interrupted_shell_command_reports_unknown_outcome_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: AsyncMock,
) -> None:
    """A shell command started before a crash is reported as uncertain and never rerun."""
    journal = tmp_path / "commands.sqlite3"
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell, journal_path=journal)
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    monkeypatch.setattr(bridge, "_execute_safely", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await _handle(bridge, _event(command))
    bridge.close()

    restarted_shell = _local_shell()
    restarted_shell.grant(60)
    restarted = _local_bridge(shell=restarted_shell, journal_path=journal)
    await _handle(restarted, _event(command))
    response = _response(transport)
    assert response.result["action_outcome"] == "unknown"
    assert "do not repeat it automatically" in str(response.result["warning"])
    assert not (tmp_path / "marker").exists()
    restarted.close()


def _without_metrics(result: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in result.items() if key != "metrics"}


async def _check_until_finished(
    bridge: DesktopBridge,
    transport: AsyncMock,
    handle: str,
    *,
    first_sequence: int,
    **parameters: object,
) -> tuple[dict[str, object], int]:
    for sequence in range(first_sequence, first_sequence + 600):
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, **parameters)))
        result = _response(transport).result
        if result["state"] != "running":
            return result, sequence
        await asyncio.sleep(0.01)
    pytest.fail("shell handle never finished")


async def _wait_until_gone(pid: int) -> None:
    for _ in range(400):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"process {pid} survived")


def _kill_recorded(pid_file: Path) -> None:
    if pid_file.exists() and pid_file.read_text().strip():
        with suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_request_status_recovers_the_reply_of_a_consumed_handle_check(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """A finished check hands its handle over once, and request_status still returns that recorded reply."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("while [ ! -f release ]; do sleep 0.05; done; printf done", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    (tmp_path / "release").touch()
    completed, sequence = await _check_until_finished(bridge, transport, handle, first_sequence=2)
    assert completed["state"] == "completed"
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence + 1)))
    assert _response(transport).error == "Unknown shell handle."

    query = _command(
        "request_status",
        request_id="query",
        sequence=sequence + 2,
        parameters={"request_id": f"check_shell-{sequence}"},
    )
    await _handle(bridge, _event(query))

    recovered = _response(transport).result
    assert recovered["state"] == "completed"
    assert recovered["response"]["result"] == completed
    assert completed["output"] == "done"
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_other_callers_cannot_kill_a_handle(transport: AsyncMock, tmp_path: Path) -> None:
    """Another allowed requester gets exactly the error reply of a handle that never existed."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("sleep 30", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    await _handle(bridge, _event(_handle_command("kill_shell", handle, sequence=2, requester_id=BOB)))
    response = _response(transport)
    assert (response.ok, response.error, _without_metrics(response.result)) == (False, "Unknown shell handle.", {})
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_local_handle_kill_reaches_the_agent_as_killed(transport: AsyncMock, tmp_path: Path) -> None:
    """After the person at the computer kills a handle, the agent's next check reports killed, not unknown."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("printf before; sleep 30", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    assert [entry["handle"] for entry in bridge.kill_local_shell_handle(handle)["shell"]["handles"]] == [handle]
    killed, _ = await _check_until_finished(bridge, transport, handle, first_sequence=2)
    assert (killed["state"], killed["exit_code"], killed["output"]) == ("killed", -signal.SIGKILL, "before")
    await bridge.stop()
    bridge.close()


@pytest.mark.parametrize("stop", ["revoke", "stop"])
@pytest.mark.asyncio
async def test_revoke_and_bridge_stop_kill_running_handles(transport: AsyncMock, tmp_path: Path, stop: str) -> None:
    """Handles outlive their inline wait but never local revocation or bridge stop."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    pid_file = tmp_path / "leader.pid"
    try:
        await _handle(bridge, _event(_run_shell(f"echo $$ > {pid_file}; sleep 30", tmp_path)))
        handle = _response(transport).result["handle"]
        assert isinstance(handle, str)
        leader = int(pid_file.read_text())
        if stop == "revoke":
            assert bridge.revoke_local_shell()["shell"]["handles"] == []
        else:
            await asyncio.wait_for(bridge.stop(), timeout=3)
        await _wait_until_gone(leader)
        if stop == "revoke":
            await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=2)))
            assert _response(transport).error == "Unknown shell handle."
            await bridge.stop()
    finally:
        _kill_recorded(pid_file)
    bridge.close()


@pytest.mark.asyncio
async def test_status_and_handle_controls_do_not_wait_behind_pending_approval(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """Approval can take two minutes, so status, checks, and kills answer while a start is pending."""
    bridge = _local_bridge(shell=_local_shell())
    responses: list[DesktopResponse] = []
    answered = asyncio.Event()

    def record(*_args: object, **kwargs: object) -> None:
        responses.append(DesktopResponse.from_content(kwargs["content"]))
        if len(responses) == 3:
            answered.set()

    transport.side_effect = record
    worker = asyncio.create_task(bridge.run())
    try:
        pending = _run_shell(PRIVATE_COMMAND, tmp_path, request_id="request-1", expires_at_ms=120_000)
        await bridge.on_to_device_event(_event(pending))
        await _wait_for_pending_shell(bridge)
        await bridge.on_to_device_event(_event(_command("status", request_id="status", sequence=2, requester_id=BOB)))
        await bridge.on_to_device_event(_event(_handle_command("check_shell", "shell:00000000", sequence=3)))
        await bridge.on_to_device_event(_event(_handle_command("kill_shell", "shell:00000000", sequence=4)))
        await asyncio.wait_for(answered.wait(), timeout=2)
        assert [response.request_id for response in responses] == ["status", "check_shell-3", "kill_shell-4"]
        assert responses[0].result["bridge"]["shell"]["pending"] is True
        assert bridge.local_status()["shell"]["pending"]["request_id"] == "request-1"
        bridge.decide_local_shell("request-1", approved=False, auto_approve_seconds=0)
    finally:
        await bridge.stop()
        await asyncio.wait_for(worker, timeout=2)
    assert not (tmp_path / "marker").exists()
    bridge.close()


@pytest.mark.asyncio
async def test_undeliverable_run_shell_reply_stays_recoverable_until_revoke_and_stop(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_status returns the first page of an undeliverable run_shell; revoke drops its handle and stop its spool."""
    monkeypatch.setattr(
        "mindroom.desktop.shell_actions.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    (tmp_path / "log").write_bytes(b"".join(f"line {number:05}\n".encode() for number in range(9_000)))
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("cat log", tmp_path)))
    first = _response(transport)
    handle = first.result["handle"]
    assert isinstance(handle, str)
    spool = Path(str(shell._directory))
    await _handle(
        bridge,
        _event(_command("request_status", request_id="query", sequence=2, parameters={"request_id": "run"})),
    )
    assert _response(transport).result["response"] == first.to_content()
    assert bridge.revoke_local_shell()["shell"]["handles"] == []
    await _handle(
        bridge,
        _event(_handle_command("check_shell", handle, sequence=3, offset=first.result["next_offset"])),
    )
    assert _response(transport).error == "Unknown shell handle."
    await bridge.stop()
    assert not spool.exists()
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("transport")
async def test_bridge_stop_during_a_stalled_output_upload_returns_within_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop drains the in-flight reply, and a stalled upload bounds that drain instead of hanging it."""
    started, released = _stall_output_uploads(monkeypatch)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    worker = asyncio.create_task(bridge.run())
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_LARGE_OUTPUT)}"
    await bridge.on_to_device_event(_event(_run_shell(command, tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.wait_for(bridge.stop(), timeout=2)
    await asyncio.wait_for(worker, timeout=2)
    _assert_upload_fallback(bridge._journal.get("run").response.result)
    assert [output.closed for output in released] == [True]
    bridge.close()


def _inline_content_bytes(command: DesktopCommand, output: str) -> int:
    """Size the exact completed reply shape the bridge measures, with the metrics room it reserves."""
    return DesktopResponse(
        request_id=command.request_id,
        session_id=command.session_id,
        ok=True,
        result={
            "state": "completed",
            "handle": None,
            "exit_code": 0,
            "output": output,
            "output_bytes": len(output.encode()),
            "output_truncated": False,
            "output_attachment": None,
            "output_start": 0,
            "next_offset": len(output.encode()),
            "metrics": _WIDEST_METRICS,
        },
    ).content_bytes()


def _largest_inline_count(command: DesktopCommand, character: str) -> int:
    count = (MAX_INLINE_RESPONSE_BYTES - _inline_content_bytes(command, "")) // len(json.dumps(character))
    while _inline_content_bytes(command, character * (count + 1)) <= MAX_INLINE_RESPONSE_BYTES:
        count += 1
    while _inline_content_bytes(command, character * count) > MAX_INLINE_RESPONSE_BYTES:
        count -= 1
    return count


@pytest.mark.parametrize("character", ["\x01", "€", "😀"], ids=["control", "bmp", "astral"])
@pytest.mark.asyncio
async def test_worst_case_escaped_output_is_inline_only_while_the_encrypted_reply_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    character: str,
) -> None:
    """Measured through real Olm encryption, the largest inline reply and its receipt stay one to-device message."""
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    uploaded: list[bytes] = []

    async def upload(_client: object, content: bytes, *, content_type: str, filename: str) -> nio.UploadResponse:
        del content_type, filename
        uploaded.append(content)
        return nio.UploadResponse("mxc://example.org/shell-output")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    desktop_user = "@" + "d" * 240 + ":example.org"
    controller_user = "@" + "c" * 240 + ":example.org"
    async with olm_transport(sender=desktop_user, recipient=controller_user) as (client, peer, requests, _):
        assert peer.olm is not None
        controller = PinnedMatrixDevice(controller_user, "DESKTOP", peer.olm.account.identity_keys["ed25519"])
        shell = _local_shell()
        shell.grant(60)
        bridge = DesktopBridge(
            client=client,
            provider=None,
            policy=replace(_policy(), controller=controller, allowed_app_ids=frozenset(), shell_enabled=True),
            shell=shell,
            clock=lambda: NOW_SECONDS,
        )
        inline_id, attached_id = "i" * _MAX_PROTOCOL_IDENTIFIER_LENGTH, "a" * _MAX_PROTOCOL_IDENTIFIER_LENGTH
        probe = replace(
            _run_shell("true", tmp_path, request_id=inline_id, expires_at_ms=120_000),
            session_id=_LONGEST_SESSION_ID,
        )
        count = _largest_inline_count(probe, character)
        results = []
        for sequence, (request_id, repeat) in enumerate(((inline_id, count), (attached_id, count + 1)), start=1):
            script = f"import sys; sys.stdout.buffer.write(({character!r} * {repeat}).encode())"
            command = _run_shell(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                tmp_path,
                request_id=request_id,
                sequence=sequence,
                timeout_seconds=30,
                expires_at_ms=120_000,
            )
            await bridge.on_to_device_event(_event(replace(command, session_id=_LONGEST_SESSION_ID)))
            await _execute(bridge)
            await bridge.deliver_pending()
            body = requests[-1]["body"]
            assert "/sendToDevice/m.room.encrypted/" in requests[-1]["path"]
            assert len(json.dumps(body, separators=(",", ":")).encode()) <= _MAX_TO_DEVICE_BYTES
            recorded = bridge._journal.get(request_id).response
            assert recorded.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
            results.append(recorded.result)
        receipt = _command(
            "request_status",
            request_id="q" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
            session_id=_LONGEST_SESSION_ID,
            sequence=3,
            parameters={"request_id": inline_id},
        )
        await bridge.on_to_device_event(_event(receipt))
        await bridge.deliver_pending()
        assert len(json.dumps(requests[-1]["body"], separators=(",", ":")).encode()) <= _MAX_TO_DEVICE_BYTES
        await bridge.stop()
        bridge.close()

    inline, attached = results
    assert (inline["output"], inline["output_attachment"]) == (character * count, None)
    assert attached["output"] == ""
    media = EncryptedDesktopMedia.from_content(attached["output_attachment"], kind="output_attachment")
    download = AsyncMock(spec=nio.AsyncClient)
    download.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_media(download, media, timeout_seconds=1) == (character * (count + 1)).encode()


@pytest.mark.asyncio
async def test_bridge_without_apps_never_starts_gui_event_pump(
    transport: AsyncMock,
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folder-only runs must work headless, without Cocoa or other GUI calls."""

    async def forbidden_pump() -> None:
        pytest.fail("GUI event pump started without application access")

    monkeypatch.setattr("mindroom.desktop.bridge._run_macos_application_events", forbidden_pump)
    bridge = _local_bridge(filesystem=DesktopFilesystem((selected_root,)))
    responded = asyncio.Event()
    transport.side_effect = lambda *_args, **_kwargs: responded.set()
    worker = asyncio.create_task(bridge.run())
    try:
        await bridge.on_to_device_event(_event(_command("list_folders")))
        await asyncio.wait_for(responded.wait(), timeout=1)
        assert _response(transport).ok
    finally:
        await bridge.stop()
        await asyncio.wait_for(worker, timeout=1)
        bridge.close()


def test_local_capabilities_require_matching_policy_and_providers(selected_root: Path) -> None:
    """A bridge needs one capability, and every enabled capability needs its own provider."""
    with pytest.raises(ValueError, match="at least one local capability"):
        replace(_policy(), allowed_app_ids=frozenset())
    files = DesktopFilesystem((selected_root,))
    no_gui = replace(_policy(), allowed_app_ids=frozenset(), allowed_file_roots=(selected_root,))
    for arguments, message in (
        ({"provider": FakeProvider(), "policy": no_gui, "filesystem": files}, "GUI provider"),
        ({"provider": None, "policy": no_gui}, "filesystem provider"),
        ({"provider": None, "policy": replace(no_gui, shell_enabled=True), "filesystem": files}, "shell provider"),
        ({"provider": FakeProvider(), "policy": _policy(), "shell": _local_shell()}, "shell provider"),
    ):
        with pytest.raises(ValueError, match=message):
            DesktopBridge(client=object(), **arguments)
    files.close()


@pytest.mark.asyncio
async def test_local_shell_controls_require_enabled_running_shell() -> None:
    """Local decisions and grants need an enabled shell on a bridge that is still accepting work."""
    gui_only = DesktopBridge(client=object(), provider=FakeProvider(), policy=_policy(), clock=lambda: NOW_SECONDS)
    for control in (
        lambda: gui_only.decide_local_shell("request-1", approved=True, auto_approve_seconds=0),
        lambda: gui_only.grant_local_shell(60),
    ):
        with pytest.raises(ValueError, match="shell access is disabled"):
            control()
    with pytest.raises(ValueError, match="shell access is disabled"):
        gui_only.revoke_local_shell()
    with pytest.raises(ValueError, match="shell access is disabled"):
        gui_only.kill_local_shell_handle("shell:1")
    assert gui_only.local_status()["shell"] == {
        "enabled": False,
        "pending": None,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    gui_only.close()
    bridge = _local_bridge(shell=_local_shell())
    assert bridge.grant_local_shell(60)["shell"]["auto_approve_remaining_seconds"] == 60
    assert bridge.grant_local_shell(until_revoked=True)["shell"]["auto_approve_until_revoked"] is True
    revoked = bridge.revoke_local_shell()["shell"]
    assert (revoked["auto_approve_remaining_seconds"], revoked["auto_approve_until_revoked"]) == (0, False)
    with pytest.raises(DesktopShellError, match="Unknown shell handle"):
        bridge.kill_local_shell_handle("shell:missing")
    await bridge.stop()
    with pytest.raises(ValueError, match="stopping"):
        bridge.grant_local_shell(60)
    bridge.close()


@pytest.mark.asyncio
async def test_bridge_refreshes_macos_apps_during_worker_launch(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launched app and its activation become visible while native work waits."""
    launch_requested = threading.Event()
    launch_published = threading.Event()
    activation_requested = threading.Event()
    activation_published = threading.Event()
    application = SimpleNamespace(
        bundleIdentifier=lambda: APP_ID,
        localizedName=lambda: "Editor",
        isActive=activation_published.is_set,
        activateWithOptions_=lambda _options: activation_requested.set() or True,
    )
    workspace = SimpleNamespace(runningApplications=lambda: [application] if launch_published.is_set() else [])

    def pump(mode: str, seconds: float, _return_after_source: bool) -> None:
        assert threading.current_thread() is threading.main_thread()
        assert mode == "default"
        assert 0 <= seconds <= 0.01
        if launch_requested.is_set():
            launch_published.set()
        if activation_requested.is_set():
            activation_published.set()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=nullcontext))
    monkeypatch.setitem(
        sys.modules,
        "CoreFoundation",
        SimpleNamespace(CFRunLoopRunInMode=pump, kCFRunLoopDefaultMode="default"),
    )
    appkit = SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "ApplicationServices", SimpleNamespace())
    monkeypatch.setattr(
        "mindroom.desktop.accessibility._request_application_activation",
        lambda _app: launch_requested.set(),
    )
    monkeypatch.setattr("mindroom.desktop.accessibility._LAUNCH_ATTEMPTS", 6)
    backend = MacAccessibilityBackend(frozenset({APP_ID}), lambda: (1920, 1080))
    provider = FakeProvider()
    monkeypatch.setattr(provider, "launch_app", backend.launch_app)
    bridge = DesktopBridge(object(), provider, _policy(allow_control=True), clock=lambda: NOW_SECONDS)
    responded = asyncio.Event()
    transport.side_effect = lambda *_args, **_kwargs: responded.set()
    worker = asyncio.create_task(bridge.run())
    try:
        await bridge.on_to_device_event(_event(_command("launch_app")))
        await asyncio.wait_for(responded.wait(), timeout=2)
        assert _response(transport).ok
        assert _response(transport).result.get("action_completed") is True
        assert backend.list_apps() == [DesktopApp(APP_ID, "Editor", True)]
        assert activation_published.is_set()
    finally:
        await bridge.stop()
        await asyncio.wait_for(worker, timeout=1)
        bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["stop", "cancel", "worker_failure"])
async def test_bridge_stops_macos_event_pump_with_workers(
    monkeypatch: pytest.MonkeyPatch,
    shutdown: str,
) -> None:
    """No Cocoa callback continues after normal, cancelled, or failed bridge exit."""
    pumped = asyncio.Event()
    ticks = 0

    def pump(_mode: str, _seconds: float, _return_after_source: bool) -> None:
        nonlocal ticks
        ticks += 1
        pumped.set()

    async def fail_worker(_bridge: DesktopBridge) -> None:
        await pumped.wait()
        msg = "worker failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=nullcontext))
    monkeypatch.setitem(
        sys.modules,
        "CoreFoundation",
        SimpleNamespace(CFRunLoopRunInMode=pump, kCFRunLoopDefaultMode="default"),
    )
    if shutdown == "worker_failure":
        monkeypatch.setattr(DesktopBridge, "_deliver_loop", fail_worker)
    bridge = DesktopBridge(object(), FakeProvider(), _policy(), clock=lambda: NOW_SECONDS)
    worker = asyncio.create_task(bridge.run())
    try:
        await asyncio.wait_for(pumped.wait(), timeout=1)
        if shutdown == "stop":
            await bridge.stop()
            await asyncio.wait_for(worker, timeout=1)
        elif shutdown == "cancel":
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        else:
            with pytest.raises(ExceptionGroup) as exc_info:
                await worker
            assert [str(error) for error in exc_info.value.exceptions] == ["worker failed"]
        final_ticks = ticks
        await asyncio.sleep(0.1)
        assert ticks == final_ticks
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        bridge.close()


@pytest.mark.asyncio
async def test_bridge_runs_without_cocoa_on_other_platforms(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable desktop actions and shutdown do not require macOS frameworks."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "CoreFoundation", None)
    bridge = DesktopBridge(object(), FakeProvider(), _policy(), clock=lambda: NOW_SECONDS)
    responded = asyncio.Event()
    transport.side_effect = lambda *_args, **_kwargs: responded.set()
    worker = asyncio.create_task(bridge.run())
    try:
        await bridge.on_to_device_event(_event(_command("list_apps")))
        await asyncio.wait_for(responded.wait(), timeout=1)
        assert _response(transport).result["apps"] == [{"id": APP_ID, "name": "Editor", "running": True}]
    finally:
        await bridge.stop()
        await asyncio.wait_for(worker, timeout=1)
        bridge.close()


@pytest.mark.asyncio
async def test_macos_application_events_reject_background_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker event loop cannot silently pump the wrong Cocoa run loop."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "CoreFoundation", None)
    with pytest.raises(RuntimeError, match="main thread"):
        await asyncio.to_thread(asyncio.run, _run_macos_application_events())


@pytest.mark.asyncio
async def test_request_status_reports_queued_work_without_executing_it(transport: AsyncMock) -> None:
    """Recovery queries bypass a blocked action executor without repeating work."""
    provider = FakeProvider()
    bridge = DesktopBridge(object(), provider, _policy(), clock=lambda: NOW_SECONDS)
    bridge._journal._max_entries = 1
    await bridge.on_to_device_event(_event(_command()))
    query = _command("request_status", request_id="query", sequence=2, parameters={"request_id": "request-1"})
    await bridge.on_to_device_event(_event(query))
    await bridge.deliver_pending()
    assert _response(transport).result == {"request_id": "request-1", "state": "queued"}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_request_status_restores_completed_result_for_original_caller(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """A new command session can recover its caller's persisted outcome."""
    provider = FakeProvider()
    path = tmp_path / "commands.sqlite3"
    bridge = DesktopBridge(object(), provider, _policy(), clock=lambda: NOW_SECONDS, journal_path=path)
    await _handle(bridge, _event(_command("status")))
    original = _response(transport)
    bridge.close()
    bridge = DesktopBridge(object(), provider, _policy(), clock=lambda: NOW_SECONDS, journal_path=path)
    query = replace(
        _command("request_status", request_id="query", sequence=2, parameters={"request_id": "request-1"}),
        session_id="recovery-session",
    )
    bridge._journal._max_entries = 1
    await bridge.on_to_device_event(_event(query))
    await bridge.deliver_pending()
    result = _response(transport).result
    assert result == {"request_id": "request-1", "state": "completed", "response": original.to_content()}
    assert provider.calls == [("status", None)]
    bridge.close()


@pytest.mark.asyncio
async def test_request_status_hides_another_allowed_callers_receipt(transport: AsyncMock) -> None:
    """Being allowlisted does not grant access to another caller's result."""
    policy = replace(_policy(), allowed_requester_ids=frozenset({"@alice:example.org", "@bob:example.org"}))
    bridge = DesktopBridge(object(), FakeProvider(), policy, clock=lambda: NOW_SECONDS)
    await bridge.on_to_device_event(_event(_command()))
    query = _command(
        "request_status",
        request_id="query",
        sequence=2,
        requester_id="@bob:example.org",
        parameters={"request_id": "request-1"},
    )
    await bridge.on_to_device_event(_event(query))
    await bridge.deliver_pending()
    assert _response(transport).result == {"request_id": "request-1", "state": "not_found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("double_click", {"x": 100, "y": 200, "button": "left"}),
        ("hover", {"x": 100, "y": 200}),
        ("drag", {"start_x": 100, "start_y": 200, "end_x": 400, "end_y": 500, "duration_ms": 500}),
        ("keypress", {"keys": ["command", "a"]}),
    ],
)
async def test_extended_inputs_keep_app_state_and_local_lease(
    transport: AsyncMock,
    action: str,
    parameters: dict[str, object],
) -> None:
    """New inputs use the same state-bound execution and fresh observation path."""
    provider = FakeProvider()
    bridge = DesktopBridge(object(), provider, _policy(allow_control=True), clock=lambda: NOW_SECONDS)
    await _handle(
        bridge,
        _event(
            _command(
                action,
                parameters={
                    "app": APP_ID,
                    "state_id": "state-1",
                    "observation": "tree",
                    **parameters,
                },
            ),
        ),
    )
    assert _response(transport).ok
    assert provider.calls[0][0] == action
    assert provider.calls[-1] == ("get_app_state", APP_ID)
    assert _response(transport).screenshot is None


@pytest.mark.asyncio
async def test_observe_only_bridge_returns_state_and_window_screenshot(transport: AsyncMock) -> None:
    """Observation returns semantic state and captures only that app window."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command()))

    response = _response(transport)
    assert response.ok
    assert response.screenshot == MEDIA
    assert response.result["state"] == STATE.to_result()
    assert response.result["capture"] == WINDOW.to_result()
    assert response.result["image"] == {"width": 800, "height": 600}
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]


@pytest.mark.asyncio
async def test_bridge_rejects_command_from_unpinned_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge wiring drops a valid command unless its Olm sender matches the exact controller pin."""
    provider = FakeProvider()
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: False)
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", send)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command()))

    assert provider.calls == []
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_apps_and_status_expose_only_coarse_local_authority(transport: AsyncMock) -> None:
    """The agent can discover allowed apps and local mode without a screenshot."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(bridge, _event(_command("list_apps")))
    assert _response(transport).result["apps"] == [DesktopApp(APP_ID, "Editor", True).to_result()]

    await _handle(bridge, _event(_command("status", request_id="request-2", sequence=2)))
    assert _response(transport).result["bridge"] == {
        "gui_mode": "control",
        "control_available": True,
        "emergency_stop_latched": False,
        "allowed_app_count": 1,
        "browser_enabled": False,
        "gui_available": True,
        "file_roots": [],
        "file_roots_truncated": False,
        "shell": {
            "enabled": False,
            "pending": False,
            "auto_approve_remaining_seconds": 0,
            "auto_approve_until_revoked": False,
            "active_request_id": None,
            "handles": [],
        },
        "control_lease_expires_at_ms": 20_000,
        "observation_modes": ["tree", "screenshot", "both"],
        "durable_commands": True,
    }


@pytest.mark.asyncio
async def test_status_names_gui_mode_so_shell_access_never_reads_as_read_only(transport: AsyncMock) -> None:
    """Observe-only describes only app control; live shell auto-approval is reported beside it."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_command("status")))
    for status in (_response(transport).result["bridge"], bridge.local_status()):
        assert "mode" not in status
        assert status["gui_mode"] == "observe_only"
        assert status["shell"]["auto_approve_remaining_seconds"] > 0
    await bridge.stop()
    assert bridge.local_status()["gui_mode"] == "stopped"
    bridge.close()


@pytest.mark.asyncio
async def test_launch_app_requires_control_and_returns_fresh_state(transport: AsyncMock) -> None:
    """Launching stays behind the local lease and returns state bound to the resulting app window."""
    provider = FakeProvider()
    observe_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(observe_bridge, _event(_command("launch_app")))

    assert _response(transport).error == "Desktop control is disabled; this bridge is observe-only."
    assert provider.calls == []
    transport.reset_mock()
    control_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(control_bridge, _event(_command("launch_app")))

    response = _response(transport)
    assert response.ok
    assert response.result["state"] == STATE.to_result()
    assert provider.calls == [
        ("launch_app", APP_ID),
        ("get_app_state", APP_ID),
        ("screenshot", (APP_ID, "state-1")),
    ]


@pytest.mark.asyncio
async def test_browser_observation_uses_optional_provider_without_control_lease(transport: AsyncMock) -> None:
    """Tabs and accessibility snapshots remain available in observe-only mode."""
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )
    command = _command(
        "browser_observe",
        parameters={"browser_action": "snapshot", "browser_parameters": {}},
    )

    await _handle(bridge, _event(command))

    response = _response(transport)
    assert response.ok
    assert response.result["provider"] == "playwright_mcp_extension"
    assert browser.calls == [("snapshot", {})]


@pytest.mark.asyncio
async def test_rejected_browser_tab_selection_does_not_upgrade_observation_to_control(transport: AsyncMock) -> None:
    """A forbidden targetId cannot turn an otherwise observational action into control."""
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "browser_control",
                parameters={"browser_action": "snapshot", "browser_parameters": {"targetId": "1"}},
            ),
        ),
    )

    assert _response(transport).error == "Desktop browser command used the wrong observe/control classification."
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_control_requires_same_local_lease_as_accessibility(transport: AsyncMock) -> None:
    """Installing the extension does not bypass the bridge's local control authority."""
    browser = FakeBrowserProvider()
    parameters = {
        "browser_action": "act",
        "browser_parameters": {"request": {"kind": "click", "ref": "e3"}},
    }
    observe_only = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await _handle(observe_only, _event(_command("browser_control", parameters=parameters)))

    assert _response(transport).error == "Desktop control is disabled; this bridge is observe-only."
    assert browser.calls == []
    transport.reset_mock()

    controlled = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )
    await _handle(controlled, _event(_command("browser_control", parameters=parameters)))

    assert _response(transport).ok
    assert browser.calls == [("act", {"request": {"kind": "click", "ref": "e3"}})]


@pytest.mark.asyncio
async def test_browser_control_honors_pointer_emergency_stop(transport: AsyncMock) -> None:
    """The local PyAutoGUI fail-safe latches before Playwright receives a control action."""
    provider = FakeProvider(emergency_stop=True)
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "browser_control",
                parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.com"}},
            ),
        ),
    )

    response = _response(transport)
    assert not response.ok
    assert "emergency stop" in (response.error or "").lower()
    assert provider.calls == [("check_emergency_stop", None)]
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_control_failure_requires_fresh_observation(transport: AsyncMock) -> None:
    """A dispatched browser mutation is never presented as safe to retry automatically."""
    browser = FakeBrowserProvider(error=PlaywrightActionOutcomeUnknownError("extension disconnected"))
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "browser_control",
                parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.com"}},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    warning = str(response.result["warning"])
    # The provider's reason comes first, ahead of the fixed recovery advice.
    assert warning.startswith("extension disconnected ")
    assert "outcome is unknown" in warning
    assert "browser(action='tabs' or 'snapshot', target='desktop')" in warning


@pytest.mark.asyncio
async def test_active_browser_request_keeps_original_response_owner(transport: AsyncMock) -> None:
    """An active request ignores exact replays while rejecting changed content."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingBrowserProvider(FakeBrowserProvider):
        async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
            entered.set()
            await release.wait()
            return await super().execute(action, parameters)

    browser = BlockingBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )
    command = _command(
        "browser_control",
        parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.org"}},
    )
    task = asyncio.create_task(_handle(bridge, _event(command)))

    try:
        await entered.wait()
        await _handle(bridge, _event(command))
        transport.assert_not_awaited()

        changed_command = _command(
            "browser_control",
            parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.net"}},
        )
        await _handle(bridge, _event(changed_command))
        assert len(transport.await_args_list) == 1
        assert "reused with different command content" in (_response(transport).error or "")
    finally:
        release.set()
        await task

    responses = [DesktopResponse.from_content(call.kwargs["content"]) for call in transport.await_args_list]
    assert len(responses) == 2
    assert responses[-1].ok
    assert responses[-1].result.get("action_outcome") != "unknown"
    assert browser.calls == [("navigate", {"targetUrl": "https://example.org"})]


@pytest.mark.asyncio
async def test_browser_screenshot_is_uploaded_as_encrypted_matrix_media(transport: AsyncMock) -> None:
    """Browser-native screenshots use the same encrypted media path as desktop captures."""
    browser = FakeBrowserProvider(
        BrowserProviderResult(
            {"action": "screenshot", "result": "captured", "status": "ok"},
            BrowserImage(b"\x89PNGbrowser", "image/png"),
        ),
    )
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "browser_observe",
                parameters={"browser_action": "screenshot", "browser_parameters": {}},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot == MEDIA
    upload = pytest.importorskip("mindroom.desktop.bridge").upload_encrypted_media
    upload.assert_awaited_once_with(
        bridge.client,
        b"\x89PNGbrowser",
        mime_type="image/png",
        filename="browser-request-1.png",
        timeout_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_disallowed_app_is_rejected_before_provider_access(transport: AsyncMock) -> None:
    """Payload parameters cannot broaden the exact local app allowlist."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command(parameters={"app": "com.example.Secret"})))

    response = _response(transport)
    assert not response.ok
    assert "local allowlist" in (response.error or "")
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issued_at_ms", "expires_at_ms", "expected_error"),
    [
        (8_000, 10_000, "expired before local execution"),
        (40_001, 41_000, "too far in the future"),
    ],
)
async def test_command_time_window_is_enforced(
    transport: AsyncMock,
    issued_at_ms: int,
    expires_at_ms: int,
    expected_error: str,
) -> None:
    """Expired and implausibly future commands fail before any local observation or input."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    command = replace(_command(), issued_at_ms=issued_at_ms, expires_at_ms=expires_at_ms)

    await _handle(bridge, _event(command))

    assert expected_error in (_response(transport).error or "")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_get_state_survives_window_screenshot_failure(transport: AsyncMock) -> None:
    """A useful accessibility tree is returned even when pixels cannot be captured."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command("get_app_state")))

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["state"] == STATE.to_result()
    assert "warning" in response.result


@pytest.mark.asyncio
async def test_screenshot_action_still_requires_pixels(transport: AsyncMock) -> None:
    """An explicit screenshot request remains a normal retryable observation failure."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command("screenshot")))

    response = _response(transport)
    assert not response.ok
    assert response.error == "Screenshot failed."


@pytest.mark.asyncio
async def test_control_is_denied_without_local_lease(transport: AsyncMock) -> None:
    """Cloud configuration alone cannot enable semantic or fallback control."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    command = _command(
        "click_element",
        parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
    )

    await _handle(bridge, _event(command))

    response = _response(transport)
    assert not response.ok
    assert response.error == "Desktop control is disabled; this bridge is observe-only."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_control_lease_uses_monotonic_deadline(transport: AsyncMock) -> None:
    """Rolling the wall clock backward cannot extend locally granted control."""
    wall_clock = [NOW_SECONDS]
    monotonic_clock = [100.0]
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: wall_clock[0],
        monotonic_clock=lambda: monotonic_clock[0],
    )
    wall_clock[0] = 5.0
    monotonic_clock[0] = 111.0

    await _handle(
        bridge,
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    assert _response(transport).error == "Local desktop control lease has expired."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_control_lease_expires_across_system_sleep(transport: AsyncMock) -> None:
    """Wall time expiry revokes control even when the macOS monotonic clock paused during sleep."""
    wall_clock = [NOW_SECONDS]
    monotonic_clock = [100.0]
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: wall_clock[0],
        monotonic_clock=lambda: monotonic_clock[0],
    )
    wall_clock[0] = 21.0
    command = replace(
        _command(
            "click",
            parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
        ),
        issued_at_ms=20_000,
        expires_at_ms=22_000,
    )

    await _handle(bridge, _event(command))

    assert _response(transport).error == "Local desktop control lease has expired."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_semantic_action_returns_fresh_state_and_window_capture(transport: AsyncMock) -> None:
    """A leased semantic action is followed by new indexes and app-scoped visual feedback."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["state"] == STATE.to_result()
    assert provider.calls == [
        ("click_element", (APP_ID, "state-1", 0)),
        ("get_app_state", APP_ID),
        ("screenshot", (APP_ID, "state-1")),
    ]


@pytest.mark.asyncio
async def test_stale_state_is_a_safe_rejection_not_unknown_input(transport: AsyncMock) -> None:
    """Local stale-state validation happens before fallback input is attempted."""
    provider = FakeProvider(stale_state=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "old", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    response = _response(transport)
    assert not response.ok
    assert "stale" in (response.error or "")


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_follow_up_state_fails(transport: AsyncMock) -> None:
    """A post-action observation failure warns against retrying known-completed input."""
    provider = FakeProvider(state_error_after=0)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert response.result["follow_up_state"] == "failed"
    assert "do not repeat" in str(response.result["warning"])


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_follow_up_capture_fails(transport: AsyncMock) -> None:
    """A capture failure after fresh state warns against retrying the action."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert response.result["follow_up_screenshot"] == "failed"


@pytest.mark.asyncio
async def test_unexpected_control_failure_reports_unknown_outcome(transport: AsyncMock) -> None:
    """An input exception cannot make a potentially completed action look retryable."""
    provider = FakeProvider(click_error=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    assert "do not repeat" in str(response.result["warning"])


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_upload_fails(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted-media upload failure cannot make completed input look retryable."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Upload failed.")),
    )
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await _handle(
        bridge,
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True


@pytest.mark.asyncio
async def test_requester_agent_replay_and_sequence_are_enforced(transport: AsyncMock) -> None:
    """Provenance and idempotency remain enforced before reading the allowed app."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await _handle(bridge, _event(_command(requester_id="@mallory:example.org")))
    assert not _response(transport).ok
    assert provider.calls == []

    await _handle(bridge, _event(_command(request_id="bad-agent", agent_name="other")))
    assert not _response(transport).ok
    assert provider.calls == []

    first = _command(request_id="request-2", sequence=2)
    await _handle(bridge, _event(first))
    first_response_content = transport.await_args.kwargs["content"]
    await _handle(bridge, _event(first))
    assert transport.await_args.kwargs["content"] == first_response_content
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]

    await _handle(bridge, _event(_command("status", request_id="request-2", sequence=2)))
    assert "reused with different command content" in (_response(transport).error or "")
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]

    await _handle(bridge, _event(_command(request_id="request-3", sequence=2)))
    assert "sequence" in (_response(transport).error or "")


@pytest.mark.asyncio
async def test_started_control_is_not_repeated_after_bridge_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: AsyncMock,
) -> None:
    """A redelivered command with an interrupted durable record returns an unknown outcome."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    provider = FakeProvider()
    command = _command(
        "click",
        parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
    )
    first_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    monkeypatch.setattr(first_bridge, "_execute_safely", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await _handle(first_bridge, _event(command))

    transport.assert_not_awaited()
    restarted_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await _handle(restarted_bridge, _event(command))

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_completed_response_is_replayed_after_bridge_restart(tmp_path: Path, transport: AsyncMock) -> None:
    """A completed durable record returns its cached response without repeating local work."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    provider = FakeProvider()
    command = _command()
    first_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await _handle(first_bridge, _event(command))
    first_response_content = transport.await_args.kwargs["content"]

    restarted_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await _handle(restarted_bridge, _event(command))

    assert transport.await_args.kwargs["content"] == first_response_content
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits are not authoritative on Windows")
@pytest.mark.asyncio
async def test_bridge_refuses_permissive_command_journal(tmp_path: Path, transport: AsyncMock) -> None:
    """A restored journal cannot expose retained desktop or browser response content."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await _handle(bridge, _event(_command()))
    transport.assert_awaited_once()
    journal_path.chmod(0o644)

    with pytest.raises(DesktopCommandJournalError, match="group or other users"):
        DesktopBridge(
            client=object(),
            provider=FakeProvider(),
            policy=_policy(),
            clock=lambda: NOW_SECONDS,
            journal_path=journal_path,
        )


@pytest.mark.asyncio
async def test_emergency_stop_latches_control_off_until_local_restart(transport: AsyncMock) -> None:
    """Moving to the fail-safe corner revokes later input in the same process."""
    provider = FakeProvider(emergency_stop=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )
    parameters = {"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"}

    await _handle(bridge, _event(_command("click", parameters=parameters)))

    assert "emergency stop" in (_response(transport).error or "")
    provider.emergency_stop = False
    await _handle(
        bridge,
        _event(_command("click", request_id="request-2", sequence=2, parameters=parameters)),
    )

    assert "latched" in (_response(transport).error or "")
    assert provider.calls == [("click", (APP_ID, "state-1", 10, 20, "left"))]


@pytest.mark.asyncio
async def test_admission_persists_without_running_the_desktop(transport: AsyncMock, tmp_path: Path) -> None:
    """The sync consumer can acknowledge an admitted command before desktop work starts."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=tmp_path / "commands.sqlite3",
    )
    await bridge.on_to_device_event(_event(_command("status")))
    assert provider.calls == []
    transport.assert_not_awaited()

    await _execute(bridge)
    assert provider.calls == [("status", None)]
    transport.assert_not_awaited()
    await bridge.deliver_pending()
    assert _response(transport).ok


@pytest.mark.asyncio
async def test_failed_response_delivery_retries_without_repeating_action(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """An unavailable Matrix connection leaves an exact result pending for a later send."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=tmp_path / "commands.sqlite3",
    )
    await bridge.on_to_device_event(_event(_command("status")))
    await _execute(bridge)
    transport.side_effect = OlmToDeviceError("offline")
    await bridge.deliver_pending()
    first_content = transport.await_args.kwargs["content"]
    transport.side_effect = None
    await bridge.deliver_pending()
    assert transport.await_args.kwargs["content"] == first_content
    assert provider.calls == [("status", None)]
    assert _response(transport).ok


@pytest.mark.asyncio
async def test_queued_command_rechecks_expiry_before_execution(transport: AsyncMock) -> None:
    """Admission does not grant permission to act after the command expires."""
    now = NOW_SECONDS
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: now)
    await bridge.on_to_device_event(_event(_command("status")))
    now += 120
    await _execute(bridge)
    await bridge.deliver_pending()
    assert provider.calls == []
    assert not _response(transport).ok
    assert "expired" in (_response(transport).error or "")


@pytest.mark.asyncio
async def test_slow_action_does_not_block_new_durable_admission(transport: AsyncMock) -> None:
    """The Matrix consumer can retain new commands while a browser action is running."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowBrowser(FakeBrowserProvider):
        async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
            entered.set()
            await release.wait()
            return await super().execute(action, parameters)

    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=SlowBrowser(),
        clock=lambda: NOW_SECONDS,
    )
    first = _command(
        "browser_control",
        parameters={"browser_action": "navigate", "browser_parameters": {"url": "https://example.org"}},
    )
    await bridge.on_to_device_event(_event(first))
    worker = asyncio.create_task(_execute(bridge))
    try:
        await entered.wait()
        await bridge.on_to_device_event(_event(first))
        await bridge.on_to_device_event(_event(_command("status", request_id="request-2", sequence=2)))
        assert [entry.command.request_id for entry in bridge._journal.queued()] == ["request-2"]
        transport.assert_not_awaited()
    finally:
        release.set()
        await worker
    await _execute(bridge)
    await bridge.deliver_pending()
    assert transport.await_count == 2


@pytest.mark.asyncio
async def test_controller_revocation_between_admission_and_execution_blocks_action(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued command must not use a device identity revoked after its admission."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    await bridge.on_to_device_event(_event(_command("status")))
    monkeypatch.setattr(
        "mindroom.desktop.bridge.resolve_pinned_device",
        AsyncMock(side_effect=OlmToDeviceError("Controller identity changed.")),
    )
    await _execute(bridge)
    await bridge.deliver_pending()
    assert provider.calls == []
    assert not _response(transport).ok


@pytest.mark.asyncio
async def test_tree_observation_skips_pixels_and_returns_fresh_semantics(transport: AsyncMock) -> None:
    """A semantic-only read must not capture or upload the desktop."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    await _handle(bridge, _event(_command("get_app_state", parameters={"app": APP_ID, "observation": "tree"})))
    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["state"]["state_id"] == "state-1"
    assert response.result["observation"]["mode"] == "tree"
    assert response.result["metrics"]["screenshot_bytes"] == 0
    assert provider.calls == [("get_app_state", APP_ID)]


@pytest.mark.asyncio
async def test_tree_followup_preserves_completed_action_without_capture(transport: AsyncMock) -> None:
    """Semantic control returns fresh state without an unnecessary image round trip."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )
    await _handle(
        bridge,
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0, "observation": "tree"},
            ),
        ),
    )
    response = _response(transport)
    assert response.ok
    assert response.result["action_completed"] is True
    assert response.screenshot is None
    assert response.result["state"]["state_id"] == "state-1"
    assert provider.calls == [("click_element", (APP_ID, "state-1", 0)), ("get_app_state", APP_ID)]


def test_local_control_grant_expiry_and_revocation() -> None:
    """Only explicit local grants create a bounded lease, and revocation is immediate."""
    now = 10.0
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(),
        clock=lambda: now,
        monotonic_clock=lambda: now,
    )
    assert bridge.local_status()["control_available"] is False
    assert bridge.grant_local_control(60)["lease_remaining_seconds"] == 60
    now += 61
    assert bridge.local_status()["control_available"] is False
    bridge.grant_local_control(60)
    assert bridge.revoke_local_control()["control_available"] is False
    for duration in (True, 0, 3601):
        with pytest.raises(ValueError, match="duration"):
            bridge.grant_local_control(duration)


@pytest.mark.asyncio
async def test_local_stop_fences_admission_without_consuming_command(transport: AsyncMock) -> None:
    """A shutting-down helper cannot silently acknowledge unexecuted commands."""
    bridge = DesktopBridge(client=object(), provider=FakeProvider(), policy=_policy(), clock=lambda: NOW_SECONDS)
    await bridge.stop()
    with pytest.raises(_DesktopBridgeStoppedError):
        await bridge.on_to_device_event(_event(_command("status")))
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_observed_opaque_ref_drives_exact_semantic_target(transport: AsyncMock) -> None:
    """A ref returned through the wire must resolve before the provider receives input."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )
    await _handle(bridge, _event(_command("get_app_state", parameters={"app": APP_ID, "observation": "tree"})))
    observed = _response(transport).result["state"]
    command = _command(
        "click_element",
        request_id="next",
        sequence=2,
        parameters={
            "app": APP_ID,
            "state_id": observed["state_id"],
            "element_ref": observed["elements"][0]["ref"],
            "observation": "tree",
        },
    )
    await bridge.on_to_device_event(_event(command))
    await _execute(bridge)
    await bridge.deliver_pending()
    assert _response(transport).ok
    assert ("click_element", (APP_ID, "state-1", 0)) in provider.calls
    assert _response(transport).result["state"]["state_id"] == "state-2"


@pytest.mark.asyncio
async def test_bridge_rejects_reference_observed_by_another_allowed_requester(transport: AsyncMock) -> None:
    """Being allowlisted does not grant access to another caller's observed targets."""
    provider = FakeProvider()
    policy = replace(
        _policy(allow_control=True),
        allowed_requester_ids=frozenset({"@alice:example.org", "@bob:example.org"}),
    )
    bridge = DesktopBridge(client=object(), provider=provider, policy=policy, clock=lambda: NOW_SECONDS)
    await _handle(bridge, _event(_command("get_app_state", parameters={"app": APP_ID, "observation": "tree"})))
    command = _command(
        "click_element",
        request_id="next",
        sequence=2,
        requester_id="@bob:example.org",
        parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
    )
    await bridge.on_to_device_event(_event(command))
    await _execute(bridge)
    await bridge.deliver_pending()
    assert not _response(transport).ok
    assert "scope" in _response(transport).error
    assert provider.calls == [("get_app_state", APP_ID)]
