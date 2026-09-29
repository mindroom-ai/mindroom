"""Shared scaffolding for desktop bridge and action tests: commands, fake providers, folders, and shells."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.accessibility import (
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopApp,
    DesktopRect,
)
from mindroom.desktop.media import upload_encrypted_media
from mindroom.desktop.protocol import DESKTOP_APP_ACTIONS, DesktopCommand, EncryptedDesktopMedia
from mindroom.desktop.provider import DesktopEmergencyStopError, DesktopProviderError, ScreenCapture
from mindroom.desktop.shell import DesktopShell, DesktopShellOutput

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.desktop.filesystem import DesktopFilesystem

NOW_SECONDS = 10.0
APP_ID = "com.example.Editor"
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


@dataclass
class FakeProvider:
    """Record the local operations requested of the GUI provider."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    emergency_stop: bool = False
    screenshot_error: bool = False
    click_error: bool = False
    stale_state: bool = False
    state_error_after: int | None = None
    state_count: int = 0

    def status(self) -> dict[str, object]:
        """Record status."""
        self.calls.append(("status", None))
        return {
            "screen": {"width": 1920, "height": 1080},
            "accessibility": {"available": True, "backend": "fake"},
        }

    def check_emergency_stop(self) -> None:
        """Model the pointer fail-safe checked before every browser control call."""
        self.calls.append(("check_emergency_stop", None))
        if self.emergency_stop:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg)

    def list_apps(self) -> list[DesktopApp]:
        """Return only the configured fake application."""
        self.calls.append(("list_apps", None))
        return [DesktopApp(APP_ID, "Editor", True)]

    def launch_app(self, app_id: str) -> None:
        """Record one exact allowlisted application launch."""
        self.calls.append(("launch_app", app_id))

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Return a fresh state or fail after the configured number of reads."""
        self.calls.append(("get_app_state", app_id))
        if self.state_error_after is not None and self.state_count >= self.state_error_after:
            msg = "App state failed."
            raise AccessibilityError(msg)
        self.state_count += 1
        return replace(STATE, state_id=f"state-{self.state_count}")

    def screenshot(self, *, app_id: str, state_id: str) -> ScreenCapture:
        """Record the exact window crop."""
        self.calls.append(("screenshot", (app_id, state_id)))
        if self.screenshot_error:
            msg = "Screenshot failed."
            raise DesktopProviderError(msg)
        return SCREENSHOT

    def click_element(self, *, app_id: str, state_id: str, element_index: int) -> None:
        """Record one semantic press."""
        self.calls.append(("click_element", (app_id, state_id, element_index)))

    def set_value(self, *, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Record one semantic value change."""
        self.calls.append(("set_value", (app_id, state_id, element_index, value)))

    def scroll_element(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        direction: str,
        pages: int,
    ) -> None:
        """Record one element-scoped scroll."""
        self.calls.append(("scroll_element", (app_id, state_id, element_index, direction, pages)))

    def perform_action(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        action_name: str,
    ) -> None:
        """Record one advertised accessibility action."""
        self.calls.append(("perform_action", (app_id, state_id, element_index, action_name)))

    def click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Record one normalized fallback click."""
        self.calls.append(("click", (app_id, state_id, x, y, button)))
        if self.emergency_stop:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg)
        if self.stale_state:
            msg = "Accessibility state is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        if self.click_error:
            msg = "Unexpected click failure."
            raise RuntimeError(msg)

    def type_text(self, *, app_id: str, state_id: str, text: str, element_index: int | None = None) -> None:
        """Record fallback text and the element it targets, if any."""
        self.calls.append(("type_text", (app_id, state_id, text, element_index)))

    def scroll(
        self,
        *,
        app_id: str,
        state_id: str,
        direction: str,
        pages: int,
        x: int | None,
        y: int | None,
    ) -> None:
        """Record fallback scroll."""
        self.calls.append(("scroll", (app_id, state_id, direction, pages, x, y)))

    def keypress(self, *, app_id: str, state_id: str, keys: list[str]) -> None:
        """Record fallback keypress."""
        self.calls.append(("keypress", (app_id, state_id, keys)))

    def double_click(self, **parameters: object) -> None:
        """Record a double click."""
        self.calls.append(("double_click", parameters))

    def hover(self, **parameters: object) -> None:
        """Record a hover."""
        self.calls.append(("hover", parameters))

    def drag(self, **parameters: object) -> None:
        """Record a drag."""
        self.calls.append(("drag", parameters))


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


def _local_shell() -> DesktopShell:
    return DesktopShell(environment={"PATH": os.defpath}, clock=lambda: NOW_SECONDS)


def _root_id(filesystem: DesktopFilesystem) -> str:
    return str(filesystem.list_folders()["folders"][0]["id"])


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


_LONGEST_SESSION_ID = "s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH


def _longest_request_id(prefix: str) -> str:
    return prefix + "x" * (_MAX_PROTOCOL_IDENTIFIER_LENGTH - len(prefix))


def _stall_output_uploads(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, list[DesktopShellOutput]]:
    """Use the real encrypted upload against a homeserver that never answers, with a short bound."""
    started = asyncio.Event()
    released: list[DesktopShellOutput] = []
    release = DesktopShellOutput.release

    async def stalled(*_args: object, **_kwargs: object) -> nio.UploadResponse:
        started.set()
        await asyncio.Event().wait()
        pytest.fail("stalled upload returned")

    def record_release(output: DesktopShellOutput) -> None:
        released.append(output)
        release(output)

    monkeypatch.setattr("mindroom.desktop.shell_actions.MEDIA_UPLOAD_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload_encrypted_media)
    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", stalled)
    monkeypatch.setattr(DesktopShellOutput, "release", record_release)
    return started, released


_LARGE_OUTPUT = "import sys; sys.stdout.write('x' * 100_000 + 'tail')"


def _assert_upload_fallback(result: dict[str, object]) -> None:
    assert (result["state"], result["exit_code"], result["output_attachment"]) == ("completed", 0, None)
    assert isinstance(result["handle"], str)
    assert (result["output_start"], set(str(result["output"]))) == (0, {"x"})
    assert result["next_offset"] == len(str(result["output"]))
    assert (result["output_bytes"], result["output_truncated"]) == (100_004, True)
    assert "upload did not finish within 0.2 seconds" in str(result["warning"])
