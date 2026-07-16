"""Hybrid accessibility and pixel provider for the local desktop bridge."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.desktop.accessibility import (
    AccessibilityBackend,
    AccessibilityState,
    DesktopApp,
    DesktopRect,
    create_accessibility_backend,
)
from mindroom.desktop.protocol import DESKTOP_SAFE_KEYS

if TYPE_CHECKING:
    from collections.abc import Callable

_EMERGENCY_STOP_MESSAGE = "Desktop emergency stop engaged; restart the bridge locally before granting control again."


class DesktopProviderError(RuntimeError):
    """One local desktop operation was rejected or failed."""


class DesktopEmergencyStopError(DesktopProviderError):
    """The local pointer fail-safe revoked input for this bridge process."""


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


class DesktopProvider(Protocol):
    """Machine-local semantic UI and bounded pixel fallback surface."""

    def status(self) -> dict[str, object]:
        """Return coarse screen, cursor, and accessibility status."""
        ...

    def list_apps(self) -> list[DesktopApp]:
        """List only applications explicitly allowed by local policy."""
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

    def type_text(self, *, app_id: str, state_id: str, text: str) -> None:
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
        return {
            "screen": {"width": int(screen.width), "height": int(screen.height)},
            "cursor": {"x": int(cursor.x), "y": int(cursor.y)},
            "accessibility": self._accessibility.availability(),
        }

    def list_apps(self) -> list[DesktopApp]:
        """List only applications explicitly allowed on this machine."""
        return self._accessibility.list_apps()

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Capture a fresh semantic state for one allowed application."""
        return self._accessibility.get_app_state(app_id)

    def screenshot(self, *, app_id: str, state_id: str) -> ScreenCapture:
        """Revalidate, foreground, crop, and downscale one allowed app as JPEG."""
        state = self._accessibility.prepare_capture(app_id, state_id)
        region = state.window
        screen_width, screen_height = self._screen_size()
        _validate_region(region, screen_width=screen_width, screen_height=screen_height)
        image = self._pyautogui.screenshot()
        source_width, source_height = image.size
        left = round(region.x * source_width / screen_width)
        top = round(region.y * source_height / screen_height)
        right = round((region.x + region.width) * source_width / screen_width)
        bottom = round((region.y + region.height) * source_height / screen_height)
        image = image.crop((left, top, right, bottom))
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
        _validate_screen_point(x, y, screen_size=self._screen_size())
        self._run_input(lambda: self._pyautogui.scroll(clicks, x=x, y=y))

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
        _validate_screen_point(screen_x, screen_y, screen_size=self._screen_size())
        self._run_input(lambda: self._pyautogui.click(x=screen_x, y=screen_y, button=button))

    def type_text(self, *, app_id: str, state_id: str, text: str) -> None:
        """Type bounded text into a freshly validated and focused app."""
        if not text or len(text) > 2000:
            msg = "text must contain between 1 and 2000 characters."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        self._accessibility.prepare_fallback(app_id, state_id)
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
        _validate_screen_point(point[0], point[1], screen_size=self._screen_size())
        clicks = _scroll_clicks(direction, pages)
        self._run_input(lambda: self._pyautogui.scroll(clicks, x=point[0], y=point[1]))

    def keypress(self, *, app_id: str, state_id: str, keys: list[str]) -> None:
        """Press one locally safe navigation key in a validated and focused app."""
        if len(keys) != 1:
            msg = "keys must contain exactly one locally safe navigation key."
            raise DesktopProviderError(msg)
        normalized = [key.strip().lower() for key in keys]
        if normalized[0] not in DESKTOP_SAFE_KEYS:
            msg = "keys contains a shortcut or key that may escape the allowed app."
            raise DesktopProviderError(msg)
        self._check_emergency_stop()
        self._accessibility.prepare_fallback(app_id, state_id)
        self._run_input(lambda: self._pyautogui.press(normalized[0]))

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


def _normalized_point(rect: DesktopRect, *, x: int, y: int) -> tuple[int, int]:
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        msg = "Fallback coordinates must be normalized integers between 0 and 1000."
        raise DesktopProviderError(msg)
    screen_x = rect.x + min(rect.width - 1, round(x * (rect.width - 1) / 1000))
    screen_y = rect.y + min(rect.height - 1, round(y * (rect.height - 1) / 1000))
    return screen_x, screen_y


def _validate_screen_point(x: int, y: int, *, screen_size: tuple[int, int]) -> None:
    width, height = screen_size
    if not 0 <= x < width or not 0 <= y < height:
        msg = f"Fallback coordinate ({x}, {y}) is outside the {width}x{height} primary screen."
        raise DesktopProviderError(msg)


def _scroll_clicks(direction: str, pages: int) -> int:
    if direction not in {"up", "down"}:
        msg = "direction must be up or down."
        raise DesktopProviderError(msg)
    if isinstance(pages, bool) or not 1 <= pages <= 10:
        msg = "pages must be between 1 and 10."
        raise DesktopProviderError(msg)
    clicks = pages * 3
    return clicks if direction == "up" else -clicks


def _rect_center(rect: DesktopRect) -> tuple[int, int]:
    return rect.x + rect.width // 2, rect.y + rect.height // 2


__all__ = [
    "DesktopEmergencyStopError",
    "DesktopProvider",
    "DesktopProviderError",
    "PyAutoGuiDesktopProvider",
    "ScreenCapture",
]
