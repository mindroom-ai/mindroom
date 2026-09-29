"""Shared scaffolding for desktop tests driven through the bridge's encrypted to-device commands."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent

from mindroom.desktop.accessibility import AccessibilityElement, AccessibilityState, DesktopRect
from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.protocol import (
    DESKTOP_APP_ACTIONS,
    DESKTOP_COMMAND_EVENT_TYPE,
    DesktopCommand,
    DesktopResponse,
    EncryptedDesktopMedia,
)
from mindroom.desktop.provider import ScreenCapture
from mindroom.desktop.shell import DesktopShell
from mindroom.matrix.olm_to_device import PinnedMatrixDevice

if TYPE_CHECKING:
    from mindroom.desktop.filesystem import DesktopFilesystem

NOW_SECONDS = 10.0
APP_ID = "com.example.Editor"
CONTROLLER = PinnedMatrixDevice("@cloud:example.org", "CLOUD", "cloud-fingerprint")
WINDOW = DesktopRect(100, 50, 800, 600)
ELEMENT = AccessibilityElement(
    index=0,
    depth=0,
    parent_index=None,
    role="AXButton",
    subrole=None,
    name="Save",
    value=None,
    enabled=True,
    settable=False,
    bounds=DesktopRect(120, 80, 80, 30),
    actions=("AXPress",),
)
STATE = AccessibilityState("state-1", APP_ID, "Editor", WINDOW, (ELEMENT,), False)
SCREENSHOT = ScreenCapture(
    b"\xff\xd8\xffimage",
    "image/jpeg",
    1920,
    1080,
    800,
    600,
    100,
    50,
    800,
    600,
)
MEDIA = EncryptedDesktopMedia(
    url="mxc://example.org/screenshot",
    key="key",
    iv="iv",
    sha256="hash",
    mime_type="image/jpeg",
    size=len(SCREENSHOT.content),
)


def _command(
    action: str = "screenshot",
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    sequence: int = 1,
    requester_id: str = "@alice:example.org",
    agent_name: str = "computer",
    parameters: dict[str, object] | None = None,
) -> DesktopCommand:
    if parameters is None:
        parameters = {"app": APP_ID} if action in DESKTOP_APP_ACTIONS else {}
    return DesktopCommand(
        request_id=request_id,
        session_id=session_id,
        sequence=sequence,
        issued_at_ms=9_000,
        expires_at_ms=11_000,
        action=action,
        requester_id=requester_id,
        agent_name=agent_name,
        parameters=parameters,
    )


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


ALICE = "@alice:example.org"
BOB = "@bob:example.org"
PRIVATE_COMMAND = "printf private-shell-text >> marker"


@pytest.fixture
def selected_root(tmp_path: Path) -> Path:
    """Create one selected folder with a text file, a subfolder, and a link that escapes it."""
    root = tmp_path / "selected"
    (root / "docs").mkdir(parents=True)
    (root / "note.txt").write_text("private text", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("# Notes\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside secret", encoding="utf-8")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    return root.resolve()


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


def _local_shell() -> DesktopShell:
    return DesktopShell(environment={"PATH": os.defpath}, clock=lambda: NOW_SECONDS)


def _root_id(filesystem: DesktopFilesystem) -> str:
    return str(filesystem.list_folders()["folders"][0]["id"])


async def _wait_for_pending_shell(bridge: DesktopBridge) -> dict[str, object]:
    for _ in range(200):
        pending = bridge.local_status()["shell"]["pending"]
        if pending is not None:
            return pending
        await asyncio.sleep(0.005)
    pytest.fail("shell approval never became pending")


# DesktopCommand/DesktopResponse identifiers are bounded to this many characters
# (protocol.py's ``_bounded_identifier``); the worst case pads both to this length.
_MAX_PROTOCOL_IDENTIFIER_LENGTH = 128


def _run_shell(
    command: str,
    cwd: Path,
    *,
    request_id: str = "run",
    sequence: int = 1,
    requester_id: str = ALICE,
    timeout_seconds: int = 1,
    expires_at_ms: int = 11_000,
) -> DesktopCommand:
    return replace(
        _command(
            "run_shell",
            request_id=request_id,
            sequence=sequence,
            requester_id=requester_id,
            parameters={"command": command, "cwd": str(cwd), "timeout_seconds": timeout_seconds},
        ),
        expires_at_ms=expires_at_ms,
    )


def _handle_command(
    action: str,
    handle: str,
    *,
    sequence: int,
    requester_id: str = ALICE,
    **parameters: object,
) -> DesktopCommand:
    return _command(
        action,
        request_id=f"{action}-{sequence}",
        sequence=sequence,
        requester_id=requester_id,
        parameters={"handle": handle, **parameters},
    )


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


_LONGEST_SESSION_ID = "s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH


def _longest_request_id(prefix: str) -> str:
    return prefix + "x" * (_MAX_PROTOCOL_IDENTIFIER_LENGTH - len(prefix))
