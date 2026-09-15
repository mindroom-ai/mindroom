"""Hybrid accessibility and pixel provider for the local desktop bridge."""

from __future__ import annotations

import io
import math
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.desktop import macos_capture, macos_input
from mindroom.desktop.accessibility import (
    AccessibilityBackend,
    AccessibilityState,
    DesktopApp,
    DesktopRect,
    create_accessibility_backend,
)
from mindroom.desktop.displays import DisplayGeometry, DisplayMappingError, display_for_region, macos_displays
from mindroom.desktop.input import DESKTOP_SCROLL_DIRECTIONS, normalize_key_chord

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image as PillowImage

_EMERGENCY_STOP_MESSAGE = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
_MACOS_UNICODE_CHUNK_LENGTH = 20


class DesktopProviderError(RuntimeError):
    """One local desktop operation was rejected or failed."""


class DesktopEmergencyStopError(DesktopProviderError):
    """The local pointer fail-safe revoked input for this bridge process."""


def request_macos_desktop_permissions() -> tuple[str, ...]:
    """Request the macOS permissions needed by the Desktop bridge."""
    if sys.platform != "darwin":
        return ()

    import ApplicationServices  # noqa: PLC0415
    import Quartz  # noqa: PLC0415

    accessibility_allowed = bool(ApplicationServices.AXIsProcessTrusted())  # ty: ignore[unresolved-attribute]
    if not accessibility_allowed:
        accessibility_allowed = bool(
            ApplicationServices.AXIsProcessTrustedWithOptions(  # ty: ignore[unresolved-attribute]
                {ApplicationServices.kAXTrustedCheckOptionPrompt: True},  # ty: ignore[unresolved-attribute]
            ),
        )

    screen_recording_allowed = bool(Quartz.CGPreflightScreenCaptureAccess())  # ty: ignore[unresolved-attribute]
    if not screen_recording_allowed:
        screen_recording_allowed = bool(Quartz.CGRequestScreenCaptureAccess())  # ty: ignore[unresolved-attribute]

    missing: list[str] = []
    if not accessibility_allowed:
        missing.append("Accessibility")
    if not screen_recording_allowed:
        missing.append("Screen Recording")
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class ScreenCapture:
    """Encoded screenshot plus its logical desktop and capture geometry."""

    content: bytes
    mime_type: str
    screen_width: int
    screen_height: int
    image_width: int
    image_height: int
    capture_x: int
    capture_y: int
    capture_width: int
    capture_height: int
    coordinates: dict[str, object] | None = None


class DesktopProvider(Protocol):
    """Machine-local semantic UI and bounded pixel fallback surface."""

    def status(self) -> dict[str, object]:
        """Return coarse screen, cursor, and accessibility status."""
        ...

    def check_emergency_stop(self) -> None:
        """Raise when the machine-local pointer fail-safe is engaged."""
        ...

    def list_apps(self) -> list[DesktopApp]:
        """List only applications explicitly allowed by local policy."""
        ...

    def launch_app(self, app_id: str) -> None:
        """Launch or foreground one exact allowlisted application."""
        ...

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Return one fresh app-scoped accessibility state."""
        ...

    def screenshot(self, *, app_id: str, state_id: str) -> ScreenCapture:
        """Revalidate and capture one app's logical desktop region."""
        ...

    def click_element(self, *, app_id: str, state_id: str, element_index: int) -> None:
        """Press one state-scoped semantic element."""
        ...

    def set_value(self, *, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Set one writable state-scoped semantic element."""
        ...

    def scroll_element(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        direction: str,
        pages: int,
    ) -> None:
        """Scroll at one state-scoped semantic element."""
        ...

    def perform_action(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        action_name: str,
    ) -> None:
        """Perform one action advertised by a state-scoped element."""
        ...

    def click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Click normalized app coordinates after revalidating state."""
        ...

    def hover(self, *, app_id: str, state_id: str, x: int, y: int) -> None:
        """Move inside the revalidated allowed window without pressing a button."""
        ...

    def double_click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Perform a bounded double click at one validated app coordinate."""
        ...

    def drag(
        self,
        *,
        app_id: str,
        state_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 500,
    ) -> None:
        """Drag within one observed window and release even if movement fails."""
        ...

    def type_text(self, *, app_id: str, state_id: str, text: str, element_index: int | None = None) -> None:
        """Type bounded text after revalidating and focusing the app."""
        ...

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
        """Scroll at normalized app coordinates after revalidating state."""
        ...

    def keypress(self, *, app_id: str, state_id: str, keys: list[str]) -> None:
        """Press a short key combination after revalidating and focusing the app."""
        ...


class PyAutoGuiDesktopProvider:
    """Accessibility-first provider with PyAutoGUI screenshots and fallback input."""

    def __init__(
        self,
        *,
        allowed_app_ids: frozenset[str],
        max_screenshot_width: int = 1600,
        jpeg_quality: int = 80,
        accessibility_backend: AccessibilityBackend | None = None,
    ) -> None:
        if not allowed_app_ids:
            msg = "Desktop provider requires at least one allowed application."
            raise ValueError(msg)
        if not 320 <= max_screenshot_width <= 3840:
            msg = "max_screenshot_width must be between 320 and 3840."
            raise ValueError(msg)
        if not 40 <= jpeg_quality <= 95:
            msg = "jpeg_quality must be between 40 and 95."
            raise ValueError(msg)
        try:
            import pyautogui  # noqa: PLC0415
        except ImportError as exc:
            msg = "Desktop bridge support is missing. Install MindRoom with the 'desktop' extra."
            raise DesktopProviderError(msg) from exc

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        self._pyautogui: Any = pyautogui
        self._capture_screen: Callable[[], PillowImage] = (
            _capture_macos_primary_screen if sys.platform == "darwin" else pyautogui.screenshot
        )
        self._capture_app_window: Callable[[int, DesktopRect], PillowImage] | None = (
            _capture_macos_window if sys.platform == "darwin" else None
        )
        self._display_geometry = macos_displays if sys.platform == "darwin" else self._primary_display_geometry
        self._max_screenshot_width = max_screenshot_width
        self._jpeg_quality = jpeg_quality
        self._accessibility = accessibility_backend or create_accessibility_backend(
            allowed_app_ids,
            self._screen_size,
        )

    def status(self) -> dict[str, object]:
        """Return coarse geometry without reading clipboard or disallowed applications."""
        screen = self._pyautogui.size()
        cursor = self._pyautogui.position()
        try:
            displays = [display.to_result() for display in self._display_geometry()]
        except DisplayMappingError as exc:
            raise DesktopProviderError(str(exc)) from exc
        return {
            "screen": {"width": int(screen.width), "height": int(screen.height)},
            "cursor": {"x": int(cursor.x), "y": int(cursor.y)},
            "accessibility": self._accessibility.availability(),
            "displays": displays,
        }

    def check_emergency_stop(self) -> None:
        """Reject control while the pointer is on a PyAutoGUI fail-safe point."""
        self._check_emergency_stop()

    def list_apps(self) -> list[DesktopApp]:
        """List only applications explicitly allowed on this machine."""
        return self._accessibility.list_apps()

    def launch_app(self, app_id: str) -> None:
        """Launch or foreground one exact allowlisted application."""
        self._check_emergency_stop()
        self._accessibility.launch_app(app_id)

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Capture a fresh semantic state for one allowed application."""
        return self._accessibility.get_app_state(app_id)

    def screenshot(self, *, app_id: str, state_id: str) -> ScreenCapture:
        """Revalidate, foreground, capture, and downscale one allowed app as JPEG."""
        target = self._accessibility.prepare_capture(app_id, state_id)
        state = target.state
        region = state.window
        screen_width, screen_height = self._screen_size()
        display = self._mapped_display(region)
        if target.process_id is None:
            _validate_region(region, screen_width=screen_width, screen_height=screen_height)
        if target.process_id is None:
            image = self._capture_screen()
            source_width, source_height = image.size
            left = round(region.x * source_width / screen_width)
            top = round(region.y * source_height / screen_height)
            right = round((region.x + region.width) * source_width / screen_width)
            bottom = round((region.y + region.height) * source_height / screen_height)
            image = image.crop((left, top, right, bottom))
        else:
            if self._capture_app_window is None:
                msg = "Window-bound capture is unavailable for this accessibility backend."
                raise DesktopProviderError(msg)
            image = self._capture_app_window(target.process_id, region)
        if self._mapped_display(region) != display:
            msg = "The display changed during capture; request fresh app state."
            raise DesktopProviderError(msg)
        source_width, source_height = image.size
        capture_scale = source_width / region.width
        if not math.isclose(capture_scale, source_height / region.height, rel_tol=0.01):
            msg = "Capture pixels do not match the logical window; request fresh app state."
            raise DesktopProviderError(msg)
        image_width, image_height = image.size
        if image_width > self._max_screenshot_width:
            scaled_height = max(1, round(image_height * self._max_screenshot_width / image_width))
            image = image.resize((self._max_screenshot_width, scaled_height))
        image_width, image_height = image.size
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=self._jpeg_quality, optimize=True)
        return ScreenCapture(
            content=output.getvalue(),
            mime_type="image/jpeg",
            screen_width=screen_width,
            screen_height=screen_height,
            image_width=image_width,
            image_height=image_height,
            capture_x=region.x,
            capture_y=region.y,
            capture_width=region.width,
            capture_height=region.height,
            coordinates={
                "display": display.to_result(),
                "logical_bounds": region.to_result(),
                "source_pixels": {"width": source_width, "height": source_height},
                "capture_scale": capture_scale,
            },
        )

    def click_element(self, *, app_id: str, state_id: str, element_index: int) -> None:
        """Invoke the element's semantic press action."""
        self._check_emergency_stop()
        self._accessibility.click_element(app_id, state_id, element_index)

    def set_value(self, *, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Set one writable semantic value without keyboard emulation."""
        if len(value) > 2000:
            msg = "value must not exceed 2000 characters."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        self._accessibility.set_value(app_id, state_id, element_index, value)

    def scroll_element(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        direction: str,
        pages: int,
    ) -> None:
        """Scroll at the center of one current semantic element."""
        self._check_emergency_stop()
        element = self._accessibility.element_for_action(app_id, state_id, element_index)
        if element.bounds is None:
            msg = f"Accessibility element {element_index} has no scrollable screen bounds."
            raise DesktopProviderError(msg)
        clicks = _scroll_clicks(direction, pages)
        x, y = _rect_center(element.bounds)
        self._mapped_display(DesktopRect(x, y, 1, 1))
        self._scroll_input(direction, clicks, x, y)

    def perform_action(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        action_name: str,
    ) -> None:
        """Invoke one action explicitly advertised in the current state."""
        self._check_emergency_stop()
        self._accessibility.perform_action(app_id, state_id, element_index, action_name)

    def click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Click normalized coordinates within a freshly validated app window."""
        if button not in {"left", "middle", "right"}:
            msg = "button must be left, middle, or right."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        state = self._accessibility.prepare_fallback(app_id, state_id)
        screen_x, screen_y = _normalized_point(state.window, x=x, y=y)
        self._mapped_display(state.window)
        self._click_input(screen_x, screen_y, button=button, count=1)

    def type_text(self, *, app_id: str, state_id: str, text: str, element_index: int | None = None) -> None:
        """Type bounded text into a validated app or an exact focused text element."""
        if not text or len(text) > 2000:
            msg = "text must contain between 1 and 2000 characters."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        guard = None
        if element_index is None:
            self._accessibility.prepare_fallback(app_id, state_id)
        else:
            if type(element_index) is not int or element_index < 0:
                msg = "element_index must be a nonnegative integer."
                raise DesktopProviderError(msg)
            guard = self._accessibility.prepare_typing(app_id, state_id, element_index)
        if sys.platform == "darwin":
            if guard is None:
                self._run_input(lambda: _type_macos_unicode(text))
            else:
                self._run_input(lambda: _type_macos_unicode(text, before_chunk=guard))
        else:
            if guard is not None:
                guard()
            self._run_input(lambda: self._pyautogui.write(text, interval=0.01))

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
        """Scroll a bounded amount at an optional normalized app coordinate."""
        if (x is None) != (y is None):
            msg = "x and y must either both be provided or both be omitted."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        state = self._accessibility.prepare_fallback(app_id, state_id)
        point = (
            _normalized_point(state.window, x=x, y=y) if x is not None and y is not None else _rect_center(state.window)
        )
        self._mapped_display(state.window)
        clicks = _scroll_clicks(direction, pages)
        self._scroll_input(direction, clicks, *point)

    def keypress(self, *, app_id: str, state_id: str, keys: list[str]) -> None:
        """Press only an explicitly allowed application editing/navigation chord."""
        try:
            normalized = normalize_key_chord(keys)
        except ValueError as exc:
            raise DesktopProviderError(str(exc)) from exc
        self._check_emergency_stop()
        self._accessibility.prepare_fallback(app_id, state_id)
        if len(normalized) == 1:
            self._run_input(lambda: self._pyautogui.press(normalized[0]))
        else:
            self._run_input(lambda: self._pyautogui.hotkey(*normalized))

    def hover(self, *, app_id: str, state_id: str, x: int, y: int) -> None:
        """Move inside the revalidated allowed window without pressing a button."""
        point = self._fallback_point(app_id, state_id, x, y)
        self._run_input(
            lambda: macos_input.move(*point) if sys.platform == "darwin" else self._pyautogui.moveTo(*point),
        )

    def double_click(self, *, app_id: str, state_id: str, x: int, y: int, button: str = "left") -> None:
        """Perform a bounded double click at one validated app coordinate."""
        if button not in {"left", "middle", "right"}:
            msg = "button must be left, middle, or right."
            raise DesktopProviderError(msg)
        point = self._fallback_point(app_id, state_id, x, y)
        self._click_input(*point, button=button, count=2)

    def drag(
        self,
        *,
        app_id: str,
        state_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 500,
    ) -> None:
        """Drag within one observed window and release even if movement fails."""
        if type(duration_ms) is not int or not 100 <= duration_ms <= 2000:
            msg = "duration_ms must be an integer between 100 and 2000."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        state = self._accessibility.prepare_fallback(app_id, state_id)
        start = _normalized_point(state.window, x=start_x, y=start_y)
        end = _normalized_point(state.window, x=end_x, y=end_y)
        self._mapped_display(state.window)
        if sys.platform == "darwin":
            self._accessibility.prepare_fallback(app_id, state_id)
            macos_input.drag(
                start,
                end,
                duration=duration_ms / 1000,
                check=self._check_emergency_stop,
                before_press=lambda: self._accessibility.prepare_fallback(app_id, state_id),
            )
            return
        self._run_input(lambda: self._pyautogui.moveTo(*start))
        self._accessibility.prepare_fallback(app_id, state_id)
        try:
            self._run_input(lambda: self._pyautogui.mouseDown(button="left"))
            self._run_input(lambda: self._pyautogui.moveTo(*end, duration=duration_ms / 1000))
        finally:
            # Release must work even when the pointer has reached the fail-safe corner.
            enabled = self._pyautogui.FAILSAFE
            try:
                self._pyautogui.FAILSAFE = False
                self._pyautogui.mouseUp(button="left")
            finally:
                self._pyautogui.FAILSAFE = enabled

    def _fallback_point(self, app_id: str, state_id: str, x: int, y: int) -> tuple[int, int]:
        self._check_emergency_stop()
        state = self._accessibility.prepare_fallback(app_id, state_id)
        point = _normalized_point(state.window, x=x, y=y)
        self._mapped_display(state.window)
        return point

    def _click_input(self, x: int, y: int, *, button: str, count: int) -> None:
        if sys.platform == "darwin":
            macos_input.click(x, y, button=button, count=count, check=self._check_emergency_stop)
        elif count == 1:
            self._run_input(lambda: self._pyautogui.click(x=x, y=y, button=button))
        else:
            self._run_input(lambda: self._pyautogui.doubleClick(x=x, y=y, button=button, interval=0.1))

    def _scroll_input(self, direction: str, clicks: int, x: int, y: int) -> None:
        if sys.platform == "darwin":
            macos_input.scroll(
                x,
                y,
                clicks=clicks,
                horizontal=direction in {"left", "right"},
                check=self._check_emergency_stop,
            )
            return
        operation = self._pyautogui.hscroll if direction in {"left", "right"} else self._pyautogui.scroll
        self._run_input(lambda: operation(clicks, x=x, y=y))

    def _primary_display_geometry(self) -> tuple[DisplayGeometry, ...]:
        width, height = self._screen_size()
        return (DisplayGeometry("primary", DesktopRect(0, 0, width, height), width, height),)

    def _mapped_display(self, region: DesktopRect) -> DisplayGeometry:
        try:
            return display_for_region(self._display_geometry(), region)
        except DisplayMappingError as exc:
            raise DesktopProviderError(str(exc)) from exc

    def _run_input(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except self._pyautogui.FailSafeException as exc:
            raise DesktopEmergencyStopError(_EMERGENCY_STOP_MESSAGE) from exc

    def _check_emergency_stop(self) -> None:
        position = self._pyautogui.position()
        point = (int(position.x), int(position.y))
        if self._pyautogui.FAILSAFE and point in self._pyautogui.FAILSAFE_POINTS:
            raise DesktopEmergencyStopError(_EMERGENCY_STOP_MESSAGE)

    def _screen_size(self) -> tuple[int, int]:
        size = self._pyautogui.size()
        return int(size.width), int(size.height)


def _validate_region(region: DesktopRect, *, screen_width: int, screen_height: int) -> None:
    if (
        region.x < 0
        or region.y < 0
        or region.width <= 0
        or region.height <= 0
        or region.x + region.width > screen_width
        or region.y + region.height > screen_height
    ):
        msg = f"Capture region is outside the {screen_width}x{screen_height} primary screen."
        raise DesktopProviderError(msg)


def _capture_macos_primary_screen() -> PillowImage:
    """Capture the primary macOS display without spawning an unbounded child process."""
    import Quartz  # noqa: PLC0415

    if not Quartz.CGPreflightScreenCaptureAccess():  # ty: ignore[unresolved-attribute]
        msg = "macOS Screen Recording permission is required for desktop screenshots."
        raise DesktopProviderError(msg)
    try:
        cg_image = macos_capture.capture_display(int(Quartz.CGMainDisplayID()))  # ty: ignore[unresolved-attribute]
    except macos_capture.MacOSCaptureError as exc:
        raise DesktopProviderError(str(exc)) from exc
    return _pillow_image_from_macos_capture(cg_image)


def _capture_macos_window(process_id: int, region: DesktopRect) -> PillowImage:
    """Capture the one on-screen Core Graphics window matching the revalidated AX target."""
    import Quartz  # noqa: PLC0415

    if not Quartz.CGPreflightScreenCaptureAccess():  # ty: ignore[unresolved-attribute]
        msg = "macOS Screen Recording permission is required for desktop screenshots."
        raise DesktopProviderError(msg)
    window_id = _macos_window_id(process_id, region)
    try:
        cg_image = macos_capture.capture_window(window_id, process_id, region)
    except macos_capture.MacOSCaptureError as exc:
        raise DesktopProviderError(str(exc)) from exc
    if _macos_window_id(process_id, region) != window_id:
        msg = "The application window changed during capture; request fresh app state."
        raise DesktopProviderError(msg)
    return _pillow_image_from_macos_capture(cg_image)


def _macos_window_id(process_id: int, region: DesktopRect) -> int:
    """Resolve the exact on-screen window without capturing unrelated pixels."""
    import Quartz  # noqa: PLC0415

    on_screen_only = Quartz.kCGWindowListOptionOnScreenOnly  # ty: ignore[unresolved-attribute]
    exclude_desktop = Quartz.kCGWindowListExcludeDesktopElements  # ty: ignore[unresolved-attribute]
    options = on_screen_only | exclude_desktop
    window_infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)  # ty: ignore[unresolved-attribute]
    matching_window_ids: list[int] = []
    for info in window_infos:
        if int(info.get(Quartz.kCGWindowOwnerPID, -1)) != process_id:  # ty: ignore[unresolved-attribute]
            continue
        if int(info.get(Quartz.kCGWindowLayer, -1)) != 0:  # ty: ignore[unresolved-attribute]
            continue
        raw_bounds = info.get(Quartz.kCGWindowBounds)  # ty: ignore[unresolved-attribute]
        if raw_bounds is None:
            continue
        converted, bounds = Quartz.CGRectMakeWithDictionaryRepresentation(raw_bounds, None)  # ty: ignore[unresolved-attribute]
        if not converted:
            continue
        candidate = DesktopRect(
            round(bounds.origin.x),
            round(bounds.origin.y),
            round(bounds.size.width),
            round(bounds.size.height),
        )
        if candidate == region:
            matching_window_ids.append(int(info[Quartz.kCGWindowNumber]))  # ty: ignore[unresolved-attribute]
    if len(matching_window_ids) != 1:
        msg = "macOS could not bind the accessibility target to one exact on-screen application window."
        raise DesktopProviderError(msg)
    return matching_window_ids[0]


def _pillow_image_from_macos_capture(cg_image: object) -> PillowImage:
    """Decode an in-memory PNG without assuming CGImage byte order or padding."""
    import AppKit  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    bitmap = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(cg_image)  # ty: ignore[unresolved-attribute]
    if bitmap is None:
        msg = "macOS could not decode the screenshot."
        raise DesktopProviderError(msg)
    content = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})  # ty: ignore[unresolved-attribute]
    if content is None:
        msg = "macOS could not encode the screenshot."
        raise DesktopProviderError(msg)
    with Image.open(io.BytesIO(bytes(content))) as image:
        return image.copy()


def _type_macos_unicode(text: str, *, before_chunk: Callable[[], None] | None = None) -> None:
    """Post layout-independent Unicode keyboard events to the active macOS app."""
    import Quartz  # noqa: PLC0415

    for offset in range(0, len(text), _MACOS_UNICODE_CHUNK_LENGTH):
        if before_chunk is not None:
            before_chunk()
        chunk = text[offset : offset + _MACOS_UNICODE_CHUNK_LENGTH]
        utf16_length = len(chunk.encode("utf-16-le")) // 2
        key_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)  # ty: ignore[unresolved-attribute]
        key_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)  # ty: ignore[unresolved-attribute]
        if key_down is None or key_up is None:
            msg = "macOS could not create a Unicode keyboard event."
            raise DesktopProviderError(msg)
        Quartz.CGEventKeyboardSetUnicodeString(key_down, utf16_length, chunk)  # ty: ignore[unresolved-attribute]
        Quartz.CGEventKeyboardSetUnicodeString(key_up, utf16_length, chunk)  # ty: ignore[unresolved-attribute]
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_down)  # ty: ignore[unresolved-attribute]
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_up)  # ty: ignore[unresolved-attribute]


def _normalized_point(rect: DesktopRect, *, x: int, y: int) -> tuple[int, int]:
    if type(x) is not int or type(y) is not int or not 0 <= x <= 1000 or not 0 <= y <= 1000:
        msg = "Fallback coordinates must be normalized integers between 0 and 1000."
        raise DesktopProviderError(msg)
    screen_x = rect.x + min(rect.width - 1, round(x * (rect.width - 1) / 1000))
    screen_y = rect.y + min(rect.height - 1, round(y * (rect.height - 1) / 1000))
    return screen_x, screen_y


def _scroll_clicks(direction: str, pages: int) -> int:
    if direction not in DESKTOP_SCROLL_DIRECTIONS:
        msg = "direction must be up, down, left, or right."
        raise DesktopProviderError(msg)
    if type(pages) is not int or not 1 <= pages <= 10:
        msg = "pages must be between 1 and 10."
        raise DesktopProviderError(msg)
    clicks = pages * 3
    return clicks if direction in {"up", "right"} else -clicks


def _rect_center(rect: DesktopRect) -> tuple[int, int]:
    return rect.x + rect.width // 2, rect.y + rect.height // 2


__all__ = [
    "DesktopEmergencyStopError",
    "DesktopProvider",
    "DesktopProviderError",
    "PyAutoGuiDesktopProvider",
    "ScreenCapture",
    "request_macos_desktop_permissions",
]
