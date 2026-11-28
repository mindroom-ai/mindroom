"""Tests for app-scoped screenshot geometry and pixel fallback input."""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest
from PIL import Image

from mindroom.desktop.accessibility import (
    AccessibilityActionOutcomeUnknownError,
    AccessibilityCapture,
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopRect,
    ElementActionTarget,
    KeyboardTarget,
)
from mindroom.desktop.displays import DisplayGeometry, DisplayMappingError
from mindroom.desktop.provider import (
    DesktopProviderError,
    PyAutoGuiDesktopProvider,
    _capture_macos_primary_screen,
    _capture_macos_window,
    _normalized_point,
    _pillow_image_from_macos_capture,
    _type_macos_unicode,
    request_macos_desktop_permissions,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _default_to_fake_pointer_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generic fake-PyAutoGUI tests off the host's real macOS input backend."""
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "linux")


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


class FakeInputError(Exception):
    """Fake PyAutoGUI input failure."""


class FakePyAutoGui:
    """Expose logical screen points separately from Retina capture pixels."""

    KEYBOARD_KEYS: ClassVar[list[str]] = ["enter", "command", "l"]

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.cursor = (10, 20)

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
        self.calls.append(("click", (x, y, button)))

    def moveTo(self, x: int, y: int, *, duration: float = 0) -> None:  # noqa: N802
        """Record the corresponding external pointer operation."""
        self.calls.append(("move", (x, y, duration)))

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
    process_id: int | None = 42
    window_id: int | None = 3
    element_bounds: DesktopRect | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)
    keyboard_focus_checks_left: int | None = None
    fallback_window_flags: list[tuple[str, bool]] = field(default_factory=list)
    include_elements_calls: list[bool] = field(default_factory=list)

    def launch_app(self, app_id: str) -> None:
        """Record one allowlisted application launch."""
        self.calls.append(("launch_app", app_id))

    def get_app_state(self, app_id: str, *, include_elements: bool = True) -> AccessibilityState:
        """Record state capture and include_elements flag."""
        self.calls.append(("get_app_state", app_id))
        self.include_elements_calls.append(include_elements)
        return AccessibilityState("state-1", app_id, "Editor", self.window, (), False)

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityCapture:
        """Record capture validation and return the current app window."""
        self.calls.append(("prepare_capture", (app_id, state_id)))
        state = AccessibilityState(state_id, app_id, "Editor", self.window, (), False)
        return AccessibilityCapture(state, self.process_id, window_id=self.window_id)

    def prepare_fallback(
        self,
        app_id: str,
        state_id: str,
        *,
        allow_window_fallback: bool = False,
    ) -> AccessibilityCapture:
        """Record validation and return the current app window and its process."""
        self.calls.append(("prepare_fallback", (app_id, state_id)))
        self.fallback_window_flags.append(("fallback", allow_window_fallback))
        state = AccessibilityState(state_id, app_id, "Editor", self.window, (), False)
        return AccessibilityCapture(state, self.process_id, window_id=self.window_id)

    def element_for_action(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> ElementActionTarget:
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
        return ElementActionTarget(element, self.process_id, window_id=self.window_id, window=self.window)

    def prepare_keyboard(
        self,
        app_id: str,
        state_id: str,
        *,
        allow_window_fallback: bool = False,
    ) -> KeyboardTarget:
        """Record validation and return a target guard that fails once its allowed checks run out."""
        self.calls.append(("prepare_keyboard", (app_id, state_id)))
        self.fallback_window_flags.append(("keyboard", allow_window_fallback))

        def guard() -> None:
            self.calls.append(("keyboard_focus", app_id))
            if self.keyboard_focus_checks_left is not None:
                if not self.keyboard_focus_checks_left:
                    msg = "The allowed application lost keyboard focus."
                    raise AccessibilityActionOutcomeUnknownError(msg)
                self.keyboard_focus_checks_left -= 1

        return KeyboardTarget(
            guard=guard,
            process_id=self.process_id,
            window_id=self.window_id,
            window=self.window,
        )

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Record semantic values, including the empty string used to clear a field."""
        self.calls.append(("set_value", (app_id, state_id, element_index, value)))


def _provider() -> tuple[PyAutoGuiDesktopProvider, FakePyAutoGui, FakeAccessibilityBackend]:
    pyautogui = FakePyAutoGui()
    accessibility = FakeAccessibilityBackend()
    provider = object.__new__(PyAutoGuiDesktopProvider)
    provider._pyautogui = pyautogui
    provider._capture_screen = pyautogui.screenshot
    provider._capture_app_window = lambda _pid, region, _wid=None: FakeImage((region.width * 2, region.height * 2))
    provider._max_screenshot_width = 1600
    provider._jpeg_quality = 80
    provider._accessibility = accessibility
    provider._display_geometry = lambda: (DisplayGeometry("primary", DesktopRect(0, 0, 1512, 982), 1512, 982),)
    return provider, pyautogui, accessibility


def test_app_capture_uses_window_bound_pixels() -> None:
    """A semantic app capture uses pixels from its exact process/window binding."""
    provider, _, _ = _provider()
    calls: list[tuple[int, DesktopRect, int | None]] = []

    def capture_window(process_id: int, region: DesktopRect, window_id: int | None = None) -> FakeImage:
        calls.append((process_id, region, window_id))
        return FakeImage((1600, 1200))

    provider._capture_app_window = capture_window

    capture = provider.screenshot(app_id="com.example.Editor", state_id="state-1")

    assert calls == [(42, DesktopRect(100, 50, 800, 600), 3)]
    assert (capture.screen_width, capture.screen_height) == (1512, 982)
    assert (capture.capture_x, capture.capture_y) == (100, 50)
    assert (capture.capture_width, capture.capture_height) == (800, 600)
    assert (capture.image_width, capture.image_height) == (1600, 1200)


def test_launch_app_uses_accessibility_backend_without_pixel_input() -> None:
    """A normal allowlisted app launch does not synthesize pixel input."""
    provider, pyautogui, accessibility = _provider()

    provider.launch_app("com.example.Editor")

    assert pyautogui.calls == []
    assert accessibility.calls == [("launch_app", "com.example.Editor")]


def test_pointer_in_screen_corner_does_not_stop_input() -> None:
    """The upper-left corner is an ordinary point; Revoke and Stop Access end control instead."""
    provider, pyautogui, accessibility = _provider()
    pyautogui.cursor = (0, 0)

    provider.launch_app("com.example.Editor")
    provider.set_value(app_id="com.example.Editor", state_id="state-1", element_index=3, value="draft")

    assert accessibility.calls == [
        ("launch_app", "com.example.Editor"),
        ("set_value", ("com.example.Editor", "state-1", 3, "draft")),
    ]


def test_capture_rejects_window_outside_primary_screen() -> None:
    """The provider does not silently expose a different monitor or crop."""
    provider, _, accessibility = _provider()
    accessibility.window = DesktopRect(-1, 0, 800, 600)

    with pytest.raises(DesktopProviderError, match="one display"):
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
    with pytest.raises(DesktopProviderError, match="not allowed"):
        provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["command", "l"])
    assert pyautogui.calls == [("press", "enter")]


def test_set_value_accepts_empty_string_to_clear_semantic_field() -> None:
    """Semantic fields can be cleared without unsafe select-all keyboard shortcuts."""
    provider, _, accessibility = _provider()

    provider.set_value(app_id="com.example.Editor", state_id="state-1", element_index=3, value="")

    assert accessibility.calls == [("set_value", ("com.example.Editor", "state-1", 3, ""))]


def test_macos_type_text_routes_apps_to_background_and_primary_screen_to_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App typing routes via background PID/window delivery; primary-screen keeps global HID Unicode."""
    provider, pyautogui, accessibility = _provider()
    bg_typed: list[tuple[int, int | None, str]] = []
    hid_typed: list[str] = []

    def type_bg(pid: int, wid: int | None, text: str, *, before_chunk: Callable[[], None]) -> None:
        before_chunk()
        bg_typed.append((pid, wid, text))

    def type_unicode(text: str, *, before_chunk: Callable[[], None]) -> None:
        before_chunk()
        hid_typed.append(text)

    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr(
        "mindroom.desktop.provider._macos_window_id",
        lambda _pid, _region, *, expected_window_id=None: 3 if expected_window_id is None else expected_window_id,
    )
    monkeypatch.setattr("mindroom.desktop.macos_input.type_text_background", type_bg)
    monkeypatch.setattr("mindroom.desktop.provider._type_macos_unicode", type_unicode)

    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="café — 漢字 🙂")
    assert bg_typed == [(42, 3, "café — 漢字 🙂")]
    assert hid_typed == []
    assert accessibility.calls[-1] == ("keyboard_focus", "com.example.Editor")

    accessibility.process_id = None
    accessibility.window_id = None
    provider.type_text(app_id="primary-screen", state_id="state-1", text="hid")
    assert hid_typed == ["hid"]
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

    _type_macos_unicode("a🙂b", before_chunk=lambda: None)

    assert configured == [(True, 4, "a🙂b"), (False, 4, "a🙂b")]
    assert posted == [True, False]


def test_macos_capture_requires_screen_recording_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wallpaper-only macOS captures are rejected before pixels can reach the agent."""
    quartz = SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: False)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    with pytest.raises(DesktopProviderError, match="Screen Recording permission"):
        _capture_macos_primary_screen()


def test_macos_desktop_permissions_are_requested_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting the bridge asks macOS for both required privacy grants."""
    accessibility_requests: list[dict[str, bool]] = []
    screen_recording_requests: list[bool] = []
    application_services = SimpleNamespace(
        AXIsProcessTrusted=lambda: False,
        AXIsProcessTrustedWithOptions=lambda options: accessibility_requests.append(options) or False,
        kAXTrustedCheckOptionPrompt="prompt",
    )
    quartz = SimpleNamespace(
        CGPreflightScreenCaptureAccess=lambda: False,
        CGRequestScreenCaptureAccess=lambda: screen_recording_requests.append(True) or False,
    )
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    assert request_macos_desktop_permissions() == ("Accessibility", "Screen Recording")
    assert accessibility_requests == [{"prompt": True}]
    assert screen_recording_requests == [True]


def test_macos_window_capture_binds_process_and_exact_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process or another window from the app cannot enter an app-scoped capture."""
    captured_window_ids: list[int] = []

    def bounds(rect: DesktopRect) -> SimpleNamespace:
        return SimpleNamespace(
            origin=SimpleNamespace(x=rect.x, y=rect.y),
            size=SimpleNamespace(width=rect.width, height=rect.height),
        )

    infos = [
        {"pid": 99, "layer": 0, "bounds": DesktopRect(100, 50, 800, 600), "number": 1},
        {"pid": 42, "layer": 0, "bounds": DesktopRect(0, 0, 50, 50), "number": 2},
        {"pid": 42, "layer": 0, "bounds": DesktopRect(100, 50, 800, 600), "number": 3},
    ]
    seen_options: list[int] = []
    quartz = SimpleNamespace(
        CGPreflightScreenCaptureAccess=lambda: True,
        CGWindowListCopyWindowInfo=lambda options, _window_id: seen_options.append(options) or infos,
        CGRectMakeWithDictionaryRepresentation=lambda value, _result: (True, bounds(value)),
        kCGWindowListOptionAll=0,
        kCGWindowListExcludeDesktopElements=2,
        kCGNullWindowID=0,
        kCGWindowOwnerPID="pid",
        kCGWindowLayer="layer",
        kCGWindowBounds="bounds",
        kCGWindowNumber="number",
    )
    expected = FakeImage((800, 600))
    monkeypatch.setattr(
        "mindroom.desktop.provider.macos_capture.capture_window",
        lambda window_id, _pid, _region: captured_window_ids.append(window_id) or "cg-image",
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr("mindroom.desktop.provider._pillow_image_from_macos_capture", lambda _image: expected)

    image = _capture_macos_window(42, DesktopRect(100, 50, 800, 600))

    assert image is expected
    assert captured_window_ids == [3]
    assert seen_options == [2, 2]  # kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements

    # Ambiguous without expected_window_id, but exact window_id disambiguates same-bounds windows (e.g. across Spaces).
    infos.append({"pid": 42, "layer": 0, "bounds": DesktopRect(100, 50, 800, 600), "number": 4})
    with pytest.raises(DesktopProviderError, match="one exact"):
        _capture_macos_window(42, DesktopRect(100, 50, 800, 600))
    assert _capture_macos_window(42, DesktopRect(100, 50, 800, 600), window_id=4) is expected
    assert captured_window_ids == [3, 4]

    # Nonzero layer (e.g. floating panel/sheet) is ignored in bounds-only fallback but allowed with exact window_id.
    panel_rect = DesktopRect(200, 120, 400, 300)
    infos.append({"pid": 42, "layer": 3, "bounds": panel_rect, "number": 7})
    with pytest.raises(DesktopProviderError, match="one exact"):
        _capture_macos_window(42, panel_rect)
    assert _capture_macos_window(42, panel_rect, window_id=7) is expected
    assert captured_window_ids == [3, 4, 7]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("hover", ("move", (100, 649, 0))),
        ("double_click", ("double_click", (100, 649, "left", 0.1))),
    ],
)
def test_extended_pointer_primitives_stay_in_validated_app(action: str, expected: object) -> None:
    """Hover and double click preserve the app-normalized state boundary."""
    provider, pointer, accessibility = _provider()
    getattr(provider, action)(app_id="com.example.Editor", state_id="state-1", x=0, y=1000)
    assert pointer.calls == [expected]
    assert accessibility.calls == [("prepare_fallback", ("com.example.Editor", "state-1"))]


def test_drag_validates_both_endpoints_before_pressing_and_releases() -> None:
    """A drag remains inside one observed window and always releases its left button."""
    provider, pointer, _ = _provider()
    provider.drag(app_id="com.example.Editor", state_id="state-1", start_x=0, start_y=0, end_x=1000, end_y=1000)
    assert pointer.calls == [("move", (100, 50, 0)), ("down", "left"), ("move", (899, 649, 0.5)), ("up", "left")]
    pointer.calls.clear()
    with pytest.raises(DesktopProviderError, match="normalized"):
        provider.drag(app_id="com.example.Editor", state_id="state-1", start_x=0, start_y=0, end_x=1001, end_y=0)
    assert pointer.calls == []


@pytest.mark.parametrize(("direction", "clicks"), [("left", -6), ("right", 6)])
def test_horizontal_scroll_preserves_axis_and_app_point(direction: str, clicks: int) -> None:
    """Horizontal scrolling must not silently become vertical input."""
    provider, pointer, _ = _provider()
    provider.scroll(app_id="com.example.Editor", state_id="state-1", direction=direction, pages=2, x=None, y=None)
    assert pointer.calls == [("hscroll", (clicks, 500, 350))]


def test_keypress_accepts_explicit_edit_chord_after_focus() -> None:
    """A safe app-local chord gets the same state/focus validation as single keys."""
    provider, pointer, accessibility = _provider()
    provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["ctrl", "a"])
    assert pointer.calls == [("hotkey", ("ctrl", "a"))]
    assert accessibility.calls == [
        ("prepare_keyboard", ("com.example.Editor", "state-1")),
        ("keyboard_focus", "com.example.Editor"),
    ]


def test_secondary_retina_capture_emits_negative_logical_origin_and_scale() -> None:
    """Secondary capture keeps logical bounds separate from source pixels."""
    provider, _, accessibility = _provider()
    accessibility.window = DesktopRect(-1800, 100, 800, 600)
    provider._display_geometry = lambda: (DisplayGeometry("2", DesktopRect(-1920, 0, 1920, 1080), 3840, 2160),)
    capture = provider.screenshot(app_id="com.example.Editor", state_id="state-1")
    assert (capture.capture_x, capture.capture_y, capture.image_width, capture.image_height) == (-1800, 100, 1600, 1200)
    assert capture.coordinates == {
        "display": {"id": "2", "bounds": {"x": -1920, "y": 0, "width": 1920, "height": 1080}, "scale": 2.0},
        "logical_bounds": {"x": -1800, "y": 100, "width": 800, "height": 600},
        "source_pixels": {"width": 1600, "height": 1200},
        "capture_scale": 2.0,
    }


def test_secondary_macos_click_routes_background_and_primary_screen_hid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative global app coordinates use background PID routing; primary-screen keeps global HID."""
    provider, pointer, accessibility = _provider()
    accessibility.window = DesktopRect(-1800, 100, 800, 600)
    provider._display_geometry = lambda: (DisplayGeometry("2", DesktopRect(-1920, 0, 1920, 1080), 3840, 2160),)
    bg_emitted: list[object] = []
    hid_emitted: list[object] = []
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr(
        "mindroom.desktop.provider._macos_window_id",
        lambda _pid, _region, *, expected_window_id=None: 3 if expected_window_id is None else expected_window_id,
    )
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.click_background",
        lambda pid, x, y, **kwargs: bg_emitted.append((pid, x, y, kwargs["count"], kwargs["window_id"])),
    )
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.click",
        lambda x, y, **kwargs: hid_emitted.append((x, y, kwargs["count"])),
    )
    provider.click(app_id="com.example.Editor", state_id="state-1", x=0, y=0, button="left")
    assert bg_emitted == [(42, -1800, 100, 1, 3)]
    assert hid_emitted == []

    accessibility.process_id = None
    accessibility.window_id = None
    provider.click(app_id="primary-screen", state_id="state-1", x=0, y=0, button="left")
    assert hid_emitted == [(-1800, 100, 1)]
    assert pointer.calls == []


def _install_occluded_background_quartz(
    monkeypatch: pytest.MonkeyPatch,
    window: DesktopRect,
) -> SimpleNamespace:
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr("mindroom.desktop.macos_input.time.sleep", lambda _delay: None)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.activate_without_raise", lambda _pid, _wid: True)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.set_integer_field", lambda *_args: False)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.set_window_location", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.post_to_pid", lambda *_args, **_kw: False)

    def bounds(rect: DesktopRect) -> SimpleNamespace:
        return SimpleNamespace(
            origin=SimpleNamespace(x=rect.x, y=rect.y),
            size=SimpleNamespace(width=rect.width, height=rect.height),
        )

    # Foreign app (pid 7) completely covers the allowed app (pid 42, wid 3).
    windows = [
        {"pid": 7, "layer": 0, "number": 9, "bounds": DesktopRect(0, 0, 1500, 900)},
        {"pid": 42, "layer": 0, "number": 3, "bounds": window},
    ]
    state = SimpleNamespace(hid_posted=[], pid_posted=[], checks=0, drop_after=None)

    def copy_windows(_options: int, _relative: int) -> list[dict[str, object]]:
        state.checks += 1
        return [windows[0]] if state.drop_after is not None and state.checks >= state.drop_after else windows

    quartz = SimpleNamespace(
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
        kCGScrollEventUnitLine=1,
        kCGWindowListOptionAll=0,
        kCGWindowListExcludeDesktopElements=2,
        kCGNullWindowID=0,
        kCGWindowLayer="layer",
        kCGWindowOwnerPID="pid",
        kCGWindowBounds="bounds",
        kCGWindowNumber="number",
        CGRectMakeWithDictionaryRepresentation=lambda value, _res: (True, bounds(value)),
        CGEventCreateMouseEvent=lambda _source, kind, point, _button: {"kind": kind, "point": point},
        CGEventSetIntegerValueField=lambda _event, _field, _value: None,
        CGEventSetFlags=lambda _event, _flags: None,
        CGEventSetLocation=lambda event, point: event.update(point=point),
        CGEventCreateScrollWheelEvent=lambda *_args: {"kind": "wheel"},
        CGEventPost=lambda _tap, event: state.hid_posted.append(event),
        CGEventPostToPid=lambda pid, event: state.pid_posted.append((pid, dict(event))),
        CGWindowListCopyWindowInfo=copy_windows,
        state=state,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return quartz


@pytest.mark.parametrize("action", ["click", "double_click", "hover", "scroll", "scroll_element", "drag"])
def test_macos_background_pointer_input_succeeds_when_occluded_and_stops_when_window_disappears(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Occluded app windows still receive PID-routed background events, but a missing/moved window stops safely."""
    provider, _, accessibility = _provider()
    quartz = _install_occluded_background_quartz(monkeypatch, accessibility.window)
    state = quartz.state

    def act() -> None:
        target = {"app_id": "com.example.Editor", "state_id": "state-1"}
        if action == "drag":
            provider.drag(**target, start_x=0, start_y=0, end_x=1000, end_y=0)
        elif action == "scroll":
            provider.scroll(**target, direction="down", pages=1, x=1000, y=0)
        elif action == "scroll_element":
            accessibility.element_bounds = DesktopRect(*_normalized_point(accessibility.window, x=1000, y=0), 1, 1)
            provider.scroll_element(**target, element_index=0, direction="down", pages=1)
        elif action == "click":
            provider.click(**target, x=1000, y=0, button="left")
        else:
            getattr(provider, action)(**target, x=1000, y=0)

    # Succeeds completely despite foreign window covering the target; never touches global HID.
    act()
    assert state.hid_posted == []
    assert state.pid_posted
    assert all(pid == 42 for pid, _ in state.pid_posted)

    # Unresolvable window before posting refuses with DesktopProviderError and posts nothing.
    state.pid_posted.clear()
    state.checks, state.drop_after = 0, 1
    with pytest.raises(DesktopProviderError, match="one exact"):
        act()
    assert state.pid_posted == []

    # Disappearing window right before first event sends nothing and raises definite AccessibilityError.
    state.pid_posted.clear()
    state.checks, state.drop_after = 0, 2
    with pytest.raises(AccessibilityError, match="nothing was sent") as caught:
        act()
    assert type(caught.value) is AccessibilityError
    assert state.pid_posted == []

    # Disappearing mid-drag raises AccessibilityActionOutcomeUnknownError and releases LeftMouseUp.
    if action == "drag":
        state.pid_posted.clear()
        state.checks, state.drop_after = 0, 5
        with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome may be partial"):
            act()
        assert state.pid_posted[-1][1]["kind"] == quartz.kCGEventLeftMouseUp


def test_macos_keypress_routes_app_to_background_and_primary_screen_to_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App keypress uses background PID delivery on macOS; primary-screen keeps PyAutoGUI global HID."""
    provider, pyautogui, accessibility = _provider()
    bg_keys: list[tuple[int, int | None, tuple[str, ...]]] = []
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr(
        "mindroom.desktop.provider._macos_window_id",
        lambda _pid, _region, *, expected_window_id=None: 3 if expected_window_id is None else expected_window_id,
    )
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.keypress_background",
        lambda pid, wid, keys: bg_keys.append((pid, wid, tuple(keys))),
    )

    provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["command", "a"])
    assert bg_keys == [(42, 3, ("command", "a"))]
    assert pyautogui.calls == []

    accessibility.process_id = None
    accessibility.window_id = None
    provider.keypress(app_id="primary-screen", state_id="state-1", keys=["enter"])
    assert pyautogui.calls == [("press", "enter")]


def test_macos_background_input_resolves_missing_window_id_and_refuses_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AX supplies window_id=None, provider resolves CGWindowID once, stamps it, and refuses if missing."""
    provider, _, accessibility = _provider()
    accessibility.window_id = None
    quartz = _install_occluded_background_quartz(monkeypatch, accessibility.window)
    state = quartz.state
    activations: list[tuple[int, int]] = []
    stamped: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.macos_skylight.activate_without_raise",
        lambda pid, wid: activations.append((pid, wid)) or True,
    )
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.macos_skylight.set_integer_field",
        lambda _event, field, value: stamped.append((field, value)) or True,
    )
    quartz.CGEventCreateKeyboardEvent = lambda _source, keycode, down: {"keycode": keycode, "down": down}
    quartz.CGEventKeyboardSetUnicodeString = lambda event, _length, text: event.update(text=text)

    provider.click(app_id="com.example.Editor", state_id="state-1", x=500, y=500, button="left")
    assert activations == [(42, 3)]
    assert {(51, 3), (91, 3), (92, 3)} <= set(stamped)

    activations.clear()
    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="hi")
    provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["command", "a"])
    assert activations == [(42, 3), (42, 3)]

    # Unresolvable window refuses pointer and keyboard actions with DesktopProviderError before posting.
    state.pid_posted.clear()
    activations.clear()
    state.checks, state.drop_after = 0, 1
    with pytest.raises(DesktopProviderError, match="one exact"):
        provider.click(app_id="com.example.Editor", state_id="state-1", x=500, y=500, button="left")
    with pytest.raises(DesktopProviderError, match="one exact"):
        provider.type_text(app_id="com.example.Editor", state_id="state-1", text="hi")
    with pytest.raises(DesktopProviderError, match="one exact"):
        provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["enter"])
    assert state.pid_posted == []
    assert activations == []


def test_drag_releases_when_motion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drag failure cannot strand a pressed button."""
    provider, pointer, _ = _provider()
    move = pointer.moveTo

    def fail_motion(x: int, y: int, *, duration: float = 0) -> None:
        if duration:
            raise FakeInputError
        move(x, y, duration=duration)

    monkeypatch.setattr(pointer, "moveTo", fail_motion)
    with pytest.raises(FakeInputError):
        provider.drag(app_id="com.example.Editor", state_id="state-1", start_x=0, start_y=0, end_x=1000, end_y=1000)
    assert pointer.calls[-1] == ("up", "left")


def test_element_targeted_typing_checks_focus_before_keyboard_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chosen field guard must run before text is emitted."""
    provider, pointer, accessibility = _provider()
    guarded = []

    def prepare(app: str, state: str, index: int) -> object:
        assert (app, state, index) == ("com.example.Editor", "state-1", 4)
        return lambda: guarded.append("focus")

    monkeypatch.setattr(accessibility, "prepare_typing", prepare, raising=False)
    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="hello", element_index=4)
    assert guarded == ["focus"]
    assert pointer.calls == [("write", ("hello", 0.01))]
    assert accessibility.calls == []


def test_unicode_stops_before_next_chunk_if_focus_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text remaining after a focus change cannot reach a different field."""
    posted = []
    quartz = SimpleNamespace(
        CGEventCreateKeyboardEvent=lambda _source, _code, down: {"down": down},
        CGEventKeyboardSetUnicodeString=lambda event, _length, text: event.update(text=text),
        CGEventPost=lambda _tap, event: posted.append(event),
        kCGHIDEventTap=0,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    guards = 0

    def guard() -> None:
        nonlocal guards
        guards += 1
        if guards > 1:
            msg = "focus changed"
            raise DesktopProviderError(msg)

    with pytest.raises(DesktopProviderError, match="focus changed"):
        _type_macos_unicode("a" * 21, before_chunk=guard)
    assert [event["text"] for event in posted] == ["a" * 20, "a" * 20]


def test_untargeted_macos_typing_stops_once_the_allowed_app_loses_keyboard_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text without an element target stops before the next chunk if the target guard fails mid-typing."""
    provider, pyautogui, accessibility = _provider()
    posted: list[dict[str, object]] = []
    quartz = SimpleNamespace(
        CGEventCreateKeyboardEvent=lambda _source, _code, down: {"down": down},
        CGEventSetFlags=lambda _event, _flags: None,
        CGEventKeyboardSetUnicodeString=lambda event, _length, text: event.update(text=text),
        CGEventPostToPid=lambda _pid, event: posted.append(event),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr(
        "mindroom.desktop.provider._macos_window_id",
        lambda _pid, _region, *, expected_window_id=None: 3 if expected_window_id is None else expected_window_id,
    )
    monkeypatch.setattr("mindroom.desktop.macos_input.time.sleep", lambda _delay: None)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.activate_without_raise", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.post_to_pid", lambda *_args, **_kw: False)
    accessibility.keyboard_focus_checks_left = 1

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="lost keyboard focus"):
        provider.type_text(app_id="com.example.Editor", state_id="state-1", text="a" * 41)

    assert [event["text"] for event in posted] == ["a" * 16, "a" * 16]
    assert accessibility.calls[0] == ("prepare_keyboard", ("com.example.Editor", "state-1"))
    assert pyautogui.calls == []


def test_untargeted_typing_checks_keyboard_focus_before_writing() -> None:
    """The portable typing path checks the same app focus right before its input."""
    provider, pyautogui, accessibility = _provider()
    accessibility.keyboard_focus_checks_left = 0

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="lost keyboard focus"):
        provider.type_text(app_id="com.example.Editor", state_id="state-1", text="rm -rf ~")

    assert pyautogui.calls == []


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x1b", "\x7f"])
def test_untargeted_typing_refuses_control_characters_before_any_input(control: str) -> None:
    """Without an exact text field, a line break could submit or run the text in whatever has focus."""
    provider, pyautogui, accessibility = _provider()

    with pytest.raises(DesktopProviderError, match="control characters; send Enter or Tab with keypress"):
        provider.type_text(app_id="com.example.Editor", state_id="state-1", text=f"rm -rf ~{control}")

    assert accessibility.calls == []
    assert pyautogui.calls == []


def test_element_targeted_typing_may_contain_line_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact, focus-checked text field can take multi-line text."""
    provider, pyautogui, accessibility = _provider()
    monkeypatch.setattr(accessibility, "prepare_typing", lambda _app, _state, _index: lambda: None, raising=False)

    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="one\ntwo", element_index=4)

    assert pyautogui.calls == [("write", ("one\ntwo", 0.01))]


def test_keypress_is_not_sent_after_the_allowed_app_loses_keyboard_focus() -> None:
    """A chord such as Enter reaches only the allowlisted app that was checked."""
    provider, pyautogui, accessibility = _provider()
    accessibility.keyboard_focus_checks_left = 0

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="lost keyboard focus"):
        provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["enter"])

    assert pyautogui.calls == []


def test_capture_rejects_display_replacement_during_capture() -> None:
    """Metadata cannot bind pixels to an OS display that changed during capture."""
    provider, _, _ = _provider()
    display = DisplayGeometry("before", DesktopRect(0, 0, 1512, 982), 1512, 982)
    provider._display_geometry = lambda: (display,)

    def capture(_process_id: int, region: DesktopRect, _window_id: int | None = None) -> FakeImage:
        nonlocal display
        display = DisplayGeometry("after", DesktopRect(0, 0, 1512, 982), 1512, 982)
        return FakeImage((region.width, region.height))

    provider._capture_app_window = capture
    with pytest.raises(DesktopProviderError, match="display changed"):
        provider.screenshot(app_id="com.example.Editor", state_id="state-1")


def test_primary_capture_uses_screencapturekit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Current macOS capture works when deprecated Core Graphics symbols are absent."""
    quartz = SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: True, CGMainDisplayID=lambda: 7)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    captured = []
    monkeypatch.setattr(
        "mindroom.desktop.provider.macos_capture.capture_display",
        lambda display_id: captured.append(display_id) or "image",
    )
    expected = FakeImage((800, 600))
    monkeypatch.setattr("mindroom.desktop.provider._pillow_image_from_macos_capture", lambda _image: expected)
    assert _capture_macos_primary_screen() is expected
    assert captured == [7]


def test_macos_image_conversion_preserves_color_without_raw_layout_assumptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CGImage byte order and padding do not dictate the decoded pixel colors."""
    output = io.BytesIO()
    Image.new("RGBA", (2, 1), (120, 30, 210, 255)).save(output, format="PNG")
    representation = SimpleNamespace(representationUsingType_properties_=lambda _format, _props: output.getvalue())
    bitmap = SimpleNamespace(initWithCGImage_=lambda _image: representation)
    appkit = SimpleNamespace(NSBitmapImageRep=SimpleNamespace(alloc=lambda: bitmap), NSBitmapImageFileTypePNG=4)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    image = _pillow_image_from_macos_capture("any-layout")
    assert image.size == (2, 1)
    assert image.getpixel((0, 0)) == (120, 30, 210, 255)


def test_window_replacement_during_screenshot_discards_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window replaced while the asynchronous capture runs cannot keep its old binding."""
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: True))
    ids = iter((3, 4))
    monkeypatch.setattr("mindroom.desktop.provider._macos_window_id", lambda _pid, _region, **_kw: next(ids))
    monkeypatch.setattr("mindroom.desktop.provider.macos_capture.capture_window", lambda *_args: "image")
    monkeypatch.setattr(
        "mindroom.desktop.provider._pillow_image_from_macos_capture",
        lambda _image: FakeImage((800, 600)),
    )
    with pytest.raises(DesktopProviderError, match="window changed"):
        _capture_macos_window(42, DesktopRect(100, 50, 800, 600))


def test_status_preserves_actionable_display_mapping_error() -> None:
    """Display enumeration failures reach the bridge's public provider-error boundary."""
    provider, _, _ = _provider()
    provider._accessibility = SimpleNamespace(availability=dict)

    def unavailable() -> tuple[DisplayGeometry, ...]:
        msg = "macOS display mapping is unavailable; reconnect displays and request fresh app state."
        raise DisplayMappingError(msg)

    called: list[str] = []
    provider._pyautogui = SimpleNamespace(
        size=lambda: called.append("size") or SimpleNamespace(width=1920, height=1080),
        position=lambda: called.append("position") or SimpleNamespace(x=0, y=0),
    )
    provider._display_geometry = unavailable
    with pytest.raises(DesktopProviderError, match="reconnect displays and request fresh app state"):
        provider.status()
    assert called == []

    # Screen/cursor probe errors are also wrapped in DesktopProviderError.
    provider._display_geometry = lambda: ()
    provider._pyautogui = SimpleNamespace(
        size=lambda: (_ for _ in ()).throw(RuntimeError("no screen available")),
        position=lambda: SimpleNamespace(x=0, y=0),
    )
    with pytest.raises(DesktopProviderError, match="no screen available"):
        provider.status()


def test_provider_accepts_jpeg_quality_20_to_95(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyAutoGuiDesktopProvider honors the same 20..95 jpeg_quality bounds as native_config."""
    fake_pyautogui = SimpleNamespace(
        FAILSAFE=True,
        PAUSE=0.0,
        size=lambda: SimpleNamespace(width=1920, height=1080),
        screenshot=lambda: FakeImage((1920, 1080)),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)
    for quality in (20, 95):
        provider = PyAutoGuiDesktopProvider(
            allowed_app_ids=frozenset({"com.example.Editor"}),
            jpeg_quality=quality,
            accessibility_backend=FakeAccessibilityBackend(),
        )
        assert provider._jpeg_quality == quality
    for invalid in (19, 96):
        with pytest.raises(ValueError, match="between 20 and 95"):
            PyAutoGuiDesktopProvider(
                allowed_app_ids=frozenset({"com.example.Editor"}),
                jpeg_quality=invalid,
                accessibility_backend=FakeAccessibilityBackend(),
            )


def test_macos_drag_does_not_call_duplicate_prepare_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drag on macOS validates once up front and once in before_press via background delivery."""
    provider, _, accessibility = _provider()
    monkeypatch.setattr("mindroom.desktop.provider.sys.platform", "darwin")
    monkeypatch.setattr(
        "mindroom.desktop.provider._macos_window_id",
        lambda _pid, _region, *, expected_window_id=None: 3 if expected_window_id is None else expected_window_id,
    )
    drag_calls: list[object] = []

    def fake_drag_bg(
        pid: int,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        duration: float,
        window_id: int | None,
        window_rect: DesktopRect | None,
        check: object,
        before_press: object,
    ) -> None:
        del duration, window_rect, check
        assert callable(before_press)
        before_press()
        drag_calls.append((pid, window_id, start, end))

    monkeypatch.setattr("mindroom.desktop.provider.macos_input.drag_background", fake_drag_bg)
    provider.drag(app_id="com.example.Editor", state_id="state-1", start_x=0, start_y=0, end_x=500, end_y=500)
    assert len(drag_calls) == 1
    assert drag_calls[0][:2] == (42, 3)
    assert [call for call in accessibility.calls if call[0] == "prepare_fallback"] == [
        ("prepare_fallback", ("com.example.Editor", "state-1")),
        ("prepare_fallback", ("com.example.Editor", "state-1")),
    ]


def test_provider_forwards_include_elements_and_allow_window_fallback() -> None:
    """Screenshot-mode state reads and screenshot-backed fallback inputs thread flags to the accessibility backend."""
    provider, _, accessibility = _provider()

    provider.get_app_state("com.example.Editor", include_elements=False)
    provider.click(
        app_id="com.example.Editor",
        state_id="state-1",
        x=500,
        y=500,
        button="left",
        allow_window_fallback=True,
    )
    provider.double_click(app_id="com.example.Editor", state_id="state-1", x=500, y=500, allow_window_fallback=True)
    provider.hover(app_id="com.example.Editor", state_id="state-1", x=500, y=500, allow_window_fallback=True)
    provider.scroll(
        app_id="com.example.Editor",
        state_id="state-1",
        direction="down",
        pages=1,
        x=None,
        y=None,
        allow_window_fallback=True,
    )
    provider.drag(
        app_id="com.example.Editor",
        state_id="state-1",
        start_x=100,
        start_y=100,
        end_x=400,
        end_y=400,
        allow_window_fallback=True,
    )
    provider.type_text(app_id="com.example.Editor", state_id="state-1", text="hi", allow_window_fallback=True)
    provider.keypress(app_id="com.example.Editor", state_id="state-1", keys=["enter"], allow_window_fallback=True)

    assert accessibility.include_elements_calls == [False]
    assert accessibility.fallback_window_flags == [
        ("fallback", True),
        ("fallback", True),
        ("fallback", True),
        ("fallback", True),
        ("fallback", True),
        ("fallback", True),
        ("keyboard", True),
        ("keyboard", True),
    ]
