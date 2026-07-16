"""Tests for app-scoped screenshot geometry and pixel fallback input."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest

from mindroom.desktop.accessibility import AccessibilityState, DesktopRect
from mindroom.desktop.provider import (
    DesktopEmergencyStopError,
    DesktopProviderError,
    PyAutoGuiDesktopProvider,
    _capture_macos_primary_screen,
    _type_macos_unicode,
)

if TYPE_CHECKING:
    import io


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
    calls: list[tuple[str, object]] = field(default_factory=list)

    def launch_app(self, app_id: str) -> None:
        """Record one allowlisted application launch."""
        self.calls.append(("launch_app", app_id))

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityState:
        """Record capture validation and return the current app window."""
        self.calls.append(("prepare_capture", (app_id, state_id)))
        return AccessibilityState(state_id, app_id, "Editor", self.window, (), False)

    def prepare_fallback(self, app_id: str, state_id: str) -> AccessibilityState:
        """Record validation and return the current app window."""
        self.calls.append(("prepare_fallback", (app_id, state_id)))
        return AccessibilityState(state_id, app_id, "Editor", self.window, (), False)

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Record semantic values, including the empty string used to clear a field."""
        self.calls.append(("set_value", (app_id, state_id, element_index, value)))


def _provider() -> tuple[PyAutoGuiDesktopProvider, FakePyAutoGui, FakeAccessibilityBackend]:
    pyautogui = FakePyAutoGui()
    accessibility = FakeAccessibilityBackend()
    provider = object.__new__(PyAutoGuiDesktopProvider)
    provider._pyautogui = pyautogui
    provider._capture_screen = pyautogui.screenshot
    provider._max_screenshot_width = 1600
    provider._jpeg_quality = 80
    provider._accessibility = accessibility
    return provider, pyautogui, accessibility


def test_retina_capture_crops_logical_app_window() -> None:
    """A logical app window maps correctly into a dense screenshot."""
    provider, _, _ = _provider()

    capture = provider.screenshot(app_id="com.example.Editor", state_id="state-1")

    assert (capture.screen_width, capture.screen_height) == (1512, 982)
    assert (capture.capture_x, capture.capture_y) == (100, 50)
    assert (capture.capture_width, capture.capture_height) == (800, 600)
    assert (capture.image_width, capture.image_height) == (1600, 1200)


def test_launch_app_checks_emergency_stop_before_accessibility_backend() -> None:
    """An app launch is local control and honors the same pointer fail-safe as input."""
    provider, pyautogui, accessibility = _provider()

    provider.launch_app("com.example.Editor")

    assert pyautogui.calls == []
    assert accessibility.calls == [("launch_app", "com.example.Editor")]


def test_capture_rejects_window_outside_primary_screen() -> None:
    """The provider does not silently expose a different monitor or crop."""
    provider, _, accessibility = _provider()
    accessibility.window = DesktopRect(-1, 0, 800, 600)

    with pytest.raises(DesktopProviderError, match="outside"):
        provider.screenshot(app_id="com.example.Editor", state_id="state-1")


def test_fallback_click_uses_normalized_app_coordinates_after_state_validation() -> None:
    """Pixel coordinates are app-relative, normalized, and bound to fresh state."""
    provider, pyautogui, accessibility = _provider()

    provider.click(app_id="com.example.Editor", state_id="state-1", x=500, y=1000, button="left")

    assert accessibility.calls == [("prepare_fallback", ("com.example.Editor", "state-1"))]
    assert pyautogui.calls == [("click", (500, 649, "left"))]


def test_fallback_rejects_unnormalized_coordinates_before_input() -> None:
    """Raw or out-of-range screen coordinates cannot reach PyAutoGUI."""
    provider, pyautogui, _ = _provider()

    with pytest.raises(DesktopProviderError, match="normalized"):
        provider.click(app_id="com.example.Editor", state_id="state-1", x=1001, y=20, button="left")

    assert pyautogui.calls == []


def test_scroll_without_coordinates_uses_allowed_window_center() -> None:
    """A default scroll cannot act wherever the user's pointer happens to be."""
    provider, pyautogui, _ = _provider()

    provider.scroll(
        app_id="com.example.Editor",
        state_id="state-1",
        direction="down",
        pages=2,
        x=None,
        y=None,
    )

    assert pyautogui.calls == [("scroll", (-6, 500, 350))]


def test_keypress_exposes_only_safe_single_navigation_keys() -> None:
    """Global shortcut chords cannot switch away from the allowlisted application."""
    provider, pyautogui, _ = _provider()

    provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["Enter"])

    assert pyautogui.calls == [("press", "enter")]
    with pytest.raises(DesktopProviderError, match="exactly one"):
        provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["command", "l"])
    assert pyautogui.calls == [("press", "enter")]


def test_set_value_accepts_empty_string_to_clear_semantic_field() -> None:
    """Semantic fields can be cleared without unsafe select-all keyboard shortcuts."""
    provider, _, accessibility = _provider()

    provider.set_value(app_id="com.example.Editor", state_id="state-1", element_index=3, value="")

    assert accessibility.calls == [("set_value", ("com.example.Editor", "state-1", 3, ""))]


def test_corner_emergency_stop_blocks_semantic_control() -> None:
    """The pointer fail-safe covers AX actions as well as PyAutoGUI input."""
    provider, pyautogui, accessibility = _provider()
    pyautogui.cursor = (0, 0)

    with pytest.raises(DesktopEmergencyStopError, match="emergency stop"):
        provider.set_value(app_id="com.example.Editor", state_id="state-1", element_index=3, value="draft")

    assert accessibility.calls == []


def test_mid_action_pyautogui_fail_safe_is_translated() -> None:
    """PyAutoGUI raising during an operation reaches the bridge as an emergency stop."""
    provider, pyautogui, _ = _provider()
    pyautogui.fail_during_input = True

    with pytest.raises(DesktopEmergencyStopError, match="emergency stop"):
        provider.click(app_id="com.example.Editor", state_id="state-1", x=10, y=20, button="left")


def test_macos_type_text_uses_layout_independent_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    """MacOS fallback typing does not use PyAutoGUI's ASCII and keyboard-layout mapping."""
    provider, pyautogui, _ = _provider()
    typed: list[str] = []
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr("mindroom.desktop.provider._type_macos_unicode", typed.append)

    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="café — 漢字 🙂")

    assert typed == ["café — 漢字 🙂"]
    assert pyautogui.calls == []


def test_macos_unicode_events_use_utf16_lengths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emoji and non-ASCII text are posted without truncating surrogate pairs."""
    configured: list[tuple[bool, int, str]] = []
    posted: list[bool] = []

    def create_event(_source: object, _keycode: int, key_down: bool) -> dict[str, bool]:
        return {"key_down": key_down}

    def set_unicode(event: dict[str, bool], length: int, text: str) -> None:
        configured.append((event["key_down"], length, text))

    def post_event(_tap: object, event: dict[str, bool]) -> None:
        posted.append(event["key_down"])

    quartz = SimpleNamespace(
        CGEventCreateKeyboardEvent=create_event,
        CGEventKeyboardSetUnicodeString=set_unicode,
        CGEventPost=post_event,
        kCGHIDEventTap="hid",
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    _type_macos_unicode("a🙂b")

    assert configured == [(True, 4, "a🙂b"), (False, 4, "a🙂b")]
    assert posted == [True, False]


def test_macos_capture_requires_screen_recording_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wallpaper-only macOS captures are rejected before pixels can reach the agent."""
    quartz = SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: False)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    with pytest.raises(DesktopProviderError, match="Screen Recording permission"):
        _capture_macos_primary_screen()
