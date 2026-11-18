"""Shared scaffolding for desktop bridge and action tests: commands, fake providers, folders, and shells."""

from __future__ import annotations

import asyncio
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.accessibility import (
    AccessibilityActionOutcomeUnknownError,
    AccessibilityCapture,
    AccessibilityElement,
    AccessibilityError,
    AccessibilityPointerPress,
    AccessibilityState,
    DesktopApp,
    DesktopRect,
)
from mindroom.desktop.bridge import DesktopBridge
from mindroom.desktop.bridge_session import DesktopBridgeSessionDeps
from mindroom.desktop.displays import DisplayGeometry
from mindroom.desktop.media import upload_encrypted_media
from mindroom.desktop.protocol import DESKTOP_APP_ACTIONS, DesktopCommand, EncryptedDesktopMedia
from mindroom.desktop.provider import (
    DesktopEmergencyStopError,
    DesktopProviderError,
    PyAutoGuiDesktopProvider,
    ScreenCapture,
)
from mindroom.desktop.session import DesktopMatrixSession
from mindroom.desktop.shell import DesktopShell, DesktopShellOutput

if TYPE_CHECKING:
    import io
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.filesystem import DesktopFilesystem
    from mindroom.matrix.device_identity import PinnedMatrixDevice

NOW_SECONDS = 10.0
APP_ID = "com.example.Editor"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
PRIVATE_COMMAND = "printf private-shell-text >> marker"
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
SAVED_SESSION = DesktopMatrixSession("https://matrix.example.org", "@desktop:example.org", "DESKTOP", "token")


class IdleSource:
    """Durable source with no Matrix traffic."""

    async def run(self) -> None:
        """Idle until cancelled."""
        await asyncio.Event().wait()

    async def wait_for_work(self) -> None:
        """Never report work."""
        await asyncio.Event().wait()


class FailingSource(IdleSource):
    """Durable source whose sync fails once the test sets `fail`."""

    def __init__(self) -> None:
        self.fail = asyncio.Event()

    async def run(self) -> None:
        """Fail the sync once `fail` is set."""
        await self.fail.wait()
        msg = "transport failed"
        raise RuntimeError(msg)


class FakeMatrixOwner:
    """Owned Matrix session stand-in over a real offline client that records opening and closing."""

    def __init__(self, lifecycle: list[str] | None = None, source: object | None = None) -> None:
        self.client = nio.AsyncClient(
            "https://matrix.example.org",
            config=nio.AsyncClientConfig(encryption_enabled=False),
        )
        self.source = source or IdleSource()
        self.lifecycle = lifecycle
        self.closes = 0
        self.closed = asyncio.Event()
        self.close_error: Exception | None = None

    async def open(self, *_args: object) -> FakeMatrixOwner:
        """Stand in for opening the saved session."""
        if self.lifecycle is not None:
            self.lifecycle.append("open")
        return self

    async def close(self) -> None:
        """Count the release, close the client, then raise `close_error` if set."""
        self.closes += 1
        if self.lifecycle is not None:
            self.lifecycle.append("owner_close")
        self.closed.set()
        await self.client.close()
        if self.close_error is not None:
            raise self.close_error


def offline_session_deps(
    owner: FakeMatrixOwner,
    prepare_client: Callable[[nio.AsyncClient, PinnedMatrixDevice], Awaitable[None]] | None = None,
) -> DesktopBridgeSessionDeps:
    """Open `owner` instead of Matrix; the controller check passes unless `prepare_client` says otherwise."""
    return DesktopBridgeSessionDeps(
        open_client=cast("Callable", owner.open),
        prepare_client=prepare_client or AsyncMock(),
    )


class ManualBridge(DesktopBridge):
    """Real bridge whose workers idle, so tests execute and deliver its commands by hand."""

    async def run(self) -> None:
        """Idle until cancelled."""
        await asyncio.Event().wait()


def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return runtime paths rooted at `tmp_path` with no environment values."""
    return cast("RuntimePaths", SimpleNamespace(storage_root=tmp_path, env_value=lambda *_: None))


@pytest.fixture
def login_environment(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the account login-shell capture with a fixed environment."""
    capture = AsyncMock(return_value={"PATH": os.environ["PATH"], "MINDROOM_CAPTURED": "from-login-shell"})
    monkeypatch.setattr("mindroom.desktop.login_environment.capture_login_environment", capture)
    return capture


def fake_cloudflare_access(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace Cloudflare Access login with a fixed token and return the application URLs it was created for."""
    applications: list[str] = []

    def create(app_url: str) -> SimpleNamespace:
        applications.append(app_url)
        return SimpleNamespace(current_token=lambda: "access-token")

    monkeypatch.setattr("mindroom.desktop.cloudflare_access.CloudflareAccessTokenProvider.create", create)
    return applications


@dataclass
class FakeProvider:
    """Record the local operations requested of the GUI provider."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    # The input gate the bridge opens and closes; None until the bridge first sets it.
    input_enabled: bool | None = None
    # The whole-computer scope the bridge last gave the provider.
    computer_scope: bool = False
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

    def set_input_enabled(self, enabled: bool) -> None:
        """Record the input gate without counting it as a provider operation."""
        self.input_enabled = enabled

    def set_computer_scope(self, active: bool) -> None:
        """Record the whole-computer scope without counting it as a provider operation."""
        self.computer_scope = active

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
    requester_id: str = ALICE,
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


@pytest.fixture
def empty_selected_root(tmp_path: Path) -> Path:
    """Create one empty selected folder."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    return root


def _local_shell() -> DesktopShell:
    return DesktopShell(environment={"PATH": os.environ["PATH"]}, clock=lambda: NOW_SECONDS)


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


# The real PyAutoGUI provider over fake PyAutoGUI, accessibility, and Quartz operating-system seams.


class FakeImage:
    """Minimal Pillow-like image exposing crop and resize geometry."""

    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def crop(self, box: tuple[int, int, int, int]) -> FakeImage:
        """Return the selected fake image region."""
        left, top, right, bottom = box
        return FakeImage((right - left, bottom - top))

    def resize(self, size: tuple[int, int]) -> FakeImage:
        """Return one resized fake image."""
        return FakeImage(size)

    def convert(self, _mode: str) -> FakeImage:
        """Return an RGB-compatible fake image."""
        return self

    def save(self, output: io.BytesIO, **_kwargs: object) -> None:
        """Write deterministic fake JPEG bytes."""
        output.write(b"jpeg")


class FakeFailSafeError(Exception):
    """Fake PyAutoGUI fail-safe exception type."""


class FakePyAutoGui:
    """Expose logical screen points separately from Retina capture pixels."""

    FailSafeException = FakeFailSafeError
    FAILSAFE = True
    FAILSAFE_POINTS: ClassVar[list[tuple[int, int]]] = [(0, 0)]
    KEYBOARD_KEYS: ClassVar[list[str]] = ["enter", "command", "l"]

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.cursor = (10, 20)
        self.fail_during_input = False

    @staticmethod
    def size() -> SimpleNamespace:
        """Return logical coordinates accepted by click and scroll."""
        return SimpleNamespace(width=1512, height=982)

    def position(self) -> SimpleNamespace:
        """Return a coarse cursor position."""
        return SimpleNamespace(x=self.cursor[0], y=self.cursor[1])

    @staticmethod
    def screenshot() -> FakeImage:
        """Return a two-times-density capture."""
        return FakeImage((3024, 1964))

    def click(self, *, x: int, y: int, button: str) -> None:
        """Record a logical click."""
        if self.fail_during_input:
            raise FakeFailSafeError
        self.calls.append(("click", (x, y, button)))

    def moveTo(self, x: int, y: int, *, duration: float = 0) -> None:  # noqa: N802
        """Record the corresponding external pointer operation."""
        self.calls.append(("move", (x, y, duration)))
        if self.fail_during_input:
            raise FakeFailSafeError

    def mouseDown(self, *, button: str) -> None:  # noqa: N802
        """Record the corresponding external pointer operation."""
        self.calls.append(("down", button))

    def mouseUp(self, *, button: str) -> None:  # noqa: N802
        """Record the corresponding external pointer operation."""
        self.calls.append(("up", button))

    def doubleClick(self, *, x: int, y: int, button: str, interval: float) -> None:  # noqa: N802
        """Record the corresponding external pointer operation."""
        self.calls.append(("double_click", (x, y, button, interval)))

    def hscroll(self, clicks: int, *, x: int, y: int) -> None:
        """Record the corresponding external pointer operation."""
        self.calls.append(("hscroll", (clicks, x, y)))

    def scroll(self, clicks: int, *, x: int | None, y: int | None) -> None:
        """Record a logical scroll."""
        self.calls.append(("scroll", (clicks, x, y)))

    def write(self, text: str, *, interval: float) -> None:
        """Record text fallback."""
        self.calls.append(("write", (text, interval)))

    def press(self, key: str) -> None:
        """Record one key."""
        self.calls.append(("press", key))

    def hotkey(self, *keys: str) -> None:
        """Record a key combination."""
        self.calls.append(("hotkey", keys))


@dataclass
class FakeAccessibilityBackend:
    """Return one state-bound app window for provider fallback tests."""

    window: DesktopRect = field(default_factory=lambda: DesktopRect(100, 50, 800, 600))
    element_bounds: DesktopRect | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)
    keyboard_focus_checks_left: int | None = None
    # None models the primary-screen target, which is bound to no application process.
    process_id: int | None = 42
    # A center click returned for a button without AXPress; None models an element pressed with AXPress.
    pointer_press: AccessibilityPointerPress | None = None

    def launch_app(self, app_id: str) -> None:
        """Record one allowlisted application launch."""
        self.calls.append(("launch_app", app_id))

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Record one observation and return the state every other fake method validates."""
        self.calls.append(("get_app_state", app_id))
        return AccessibilityState("state-1", app_id, "Editor", self.window, (), False)

    def set_computer_scope(self, active: bool) -> None:
        """Record the whole-computer scope the provider forwards."""
        self.calls.append(("set_computer_scope", active))

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityCapture:
        """Record capture validation and return the current app window."""
        self.calls.append(("prepare_capture", (app_id, state_id)))
        state = AccessibilityState(state_id, app_id, "Editor", self.window, (), False)
        return AccessibilityCapture(state, self.process_id)

    def prepare_fallback(self, app_id: str, state_id: str) -> AccessibilityCapture:
        """Record validation and return the current app window and its process."""
        self.calls.append(("prepare_fallback", (app_id, state_id)))
        state = AccessibilityState(state_id, app_id, "Editor", self.window, (), False)
        return AccessibilityCapture(state, self.process_id)

    def element_for_action(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> tuple[AccessibilityElement, int | None]:
        """Record validation and return one element at the configured bounds and its process."""
        self.calls.append(("element_for_action", (app_id, state_id, element_index)))
        element = AccessibilityElement(
            index=element_index,
            depth=0,
            parent_index=None,
            role="AXScrollArea",
            subrole=None,
            name=None,
            value=None,
            enabled=True,
            settable=False,
            bounds=self.element_bounds,
            actions=(),
        )
        return element, self.process_id

    def click_element(self, app_id: str, state_id: str, element_index: int) -> AccessibilityPointerPress | None:
        """Record one press and return the configured center click, if any, for the provider to post."""
        self.calls.append(("click_element", (app_id, state_id, element_index)))
        return self.pointer_press

    def prepare_keyboard(self, app_id: str, state_id: str) -> Callable[[], None]:
        """Record validation and return a focus guard that fails once its allowed checks run out."""
        self.calls.append(("prepare_keyboard", (app_id, state_id)))

        def guard() -> None:
            self.calls.append(("keyboard_focus", app_id))
            if self.keyboard_focus_checks_left is not None:
                if not self.keyboard_focus_checks_left:
                    msg = "The allowed application lost keyboard focus."
                    raise AccessibilityActionOutcomeUnknownError(msg)
                self.keyboard_focus_checks_left -= 1

        return guard

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Record semantic values, including the empty string used to clear a field."""
        self.calls.append(("set_value", (app_id, state_id, element_index, value)))


def _provider() -> tuple[PyAutoGuiDesktopProvider, FakePyAutoGui, FakeAccessibilityBackend]:
    pyautogui = FakePyAutoGui()
    accessibility = FakeAccessibilityBackend()
    provider = object.__new__(PyAutoGuiDesktopProvider)
    provider._pyautogui = pyautogui
    provider._capture_screen = pyautogui.screenshot
    provider._capture_app_window = lambda _process_id, region: FakeImage((region.width * 2, region.height * 2))
    provider._max_screenshot_width = 1600
    provider._jpeg_quality = 80
    provider._accessibility = accessibility
    provider._display_geometry = lambda: (DisplayGeometry("primary", DesktopRect(0, 0, 1512, 982), 1512, 982),)
    provider._main_display = provider._screen_display
    provider._primary_displays = OrderedDict()
    provider._input_enabled = threading.Event()
    provider.set_input_enabled(True)
    return provider, pyautogui, accessibility


def _window(pid: int, layer: int, x: int, width: int, alpha: float = 1.0) -> dict[str, object]:
    bounds = {"X": x, "Y": 0, "Width": width, "Height": 1000}
    return {"layer": layer, "alpha": alpha, "pid": pid, "bounds": bounds}


def _fake_quartz(posted: list[dict[str, object]], windows: list[dict[str, object]]) -> SimpleNamespace:
    """Record posted pointer events and list `windows` front to back as Quartz would."""
    return SimpleNamespace(
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGEventLeftMouseDragged=3,
        kCGEventRightMouseDown=4,
        kCGEventRightMouseUp=5,
        kCGEventOtherMouseDown=6,
        kCGEventOtherMouseUp=7,
        kCGEventMouseMoved=8,
        kCGMouseButtonLeft=0,
        kCGMouseButtonRight=1,
        kCGMouseButtonCenter=2,
        kCGMouseEventClickState=10,
        kCGHIDEventTap=0,
        kCGScrollEventUnitLine=1,
        kCGWindowListOptionOnScreenOnly=1,
        kCGNullWindowID=0,
        kCGWindowLayer="layer",
        kCGWindowAlpha="alpha",
        kCGWindowOwnerPID="pid",
        kCGWindowBounds="bounds",
        CGEventCreateMouseEvent=lambda _source, kind, point, _button: {"kind": kind, "point": point},
        CGEventSetIntegerValueField=lambda _event, _field, _value: None,
        CGEventCreateScrollWheelEvent=lambda *_args: {"kind": "wheel"},
        CGEventPost=lambda _tap, event: posted.append(event),
        CGWindowListCopyWindowInfo=lambda _options, _relative: windows,
    )
