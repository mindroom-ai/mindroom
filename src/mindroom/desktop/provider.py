"""Local desktop provider contract and the optional PyAutoGUI implementation."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class DesktopProviderError(RuntimeError):
    """One local desktop operation was rejected or failed."""


class DesktopEmergencyStopError(DesktopProviderError):
    """The local pointer fail-safe revoked input for this bridge process."""


@dataclass(frozen=True, slots=True)
class ScreenCapture:
    """Encoded screenshot plus its source-screen dimensions."""

    content: bytes
    mime_type: str
    screen_width: int
    screen_height: int
    image_width: int
    image_height: int


class DesktopProvider(Protocol):
    """Small machine-local surface exposed by the Matrix bridge."""

    def status(self) -> dict[str, object]:
        """Return coarse screen and cursor state."""
        ...

    def screenshot(self) -> ScreenCapture:
        """Capture the current desktop."""
        ...

    def click(self, *, x: int, y: int, button: str) -> None:
        """Click one screen coordinate."""
        ...

    def type_text(self, *, text: str) -> None:
        """Type text into the focused application."""
        ...

    def scroll(self, *, clicks: int, x: int | None, y: int | None) -> None:
        """Scroll at the current or supplied location."""
        ...

    def keypress(self, *, keys: list[str]) -> None:
        """Press one key or short combination."""
        ...


class PyAutoGuiDesktopProvider:
    """Cross-platform screenshot and input provider with PyAutoGUI fail-safe enabled."""

    def __init__(self, *, max_screenshot_width: int = 1600, jpeg_quality: int = 80) -> None:
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

    def status(self) -> dict[str, object]:
        """Return coarse geometry without reading clipboard or application contents."""
        screen = self._pyautogui.size()
        cursor = self._pyautogui.position()
        return {
            "screen": {"width": int(screen.width), "height": int(screen.height)},
            "cursor": {"x": int(cursor.x), "y": int(cursor.y)},
        }

    def screenshot(self) -> ScreenCapture:
        """Capture and downscale the current desktop as a JPEG."""
        screen_width, screen_height = self._screen_size()
        image = self._pyautogui.screenshot()
        source_width, source_height = image.size
        if source_width > self._max_screenshot_width:
            scaled_height = max(1, round(source_height * self._max_screenshot_width / source_width))
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
        )

    def click(self, *, x: int, y: int, button: str) -> None:
        """Click a validated coordinate on the current desktop."""
        width, height = self._screen_size()
        if not 0 <= x < width or not 0 <= y < height:
            msg = f"Click coordinate ({x}, {y}) is outside the {width}x{height} desktop."
            raise DesktopProviderError(msg)
        if button not in {"left", "middle", "right"}:
            msg = "button must be left, middle, or right."
            raise DesktopProviderError(msg)
        self._run_input(lambda: self._pyautogui.click(x=x, y=y, button=button))

    def type_text(self, *, text: str) -> None:
        """Type bounded text into the focused application."""
        if not text or len(text) > 2000:
            msg = "text must contain between 1 and 2000 characters."
            raise DesktopProviderError(msg)
        self._run_input(lambda: self._pyautogui.write(text, interval=0.01))

    def scroll(self, *, clicks: int, x: int | None, y: int | None) -> None:
        """Scroll a bounded amount, optionally at a validated coordinate."""
        if clicks == 0 or not -50 <= clicks <= 50:
            msg = "clicks must be between -50 and 50 and cannot be zero."
            raise DesktopProviderError(msg)
        if (x is None) != (y is None):
            msg = "x and y must either both be provided or both be omitted."
            raise DesktopProviderError(msg)
        if x is not None and y is not None:
            width, height = self._screen_size()
            if not 0 <= x < width or not 0 <= y < height:
                msg = f"Scroll coordinate ({x}, {y}) is outside the {width}x{height} desktop."
                raise DesktopProviderError(msg)
        self._run_input(lambda: self._pyautogui.scroll(clicks, x=x, y=y))

    def keypress(self, *, keys: list[str]) -> None:
        """Press one key or a short key combination."""
        if not 1 <= len(keys) <= 4:
            msg = "keys must contain between 1 and 4 key names."
            raise DesktopProviderError(msg)
        normalized = [key.strip().lower() for key in keys]
        supported = set(self._pyautogui.KEYBOARD_KEYS)
        if any(not key or key not in supported for key in normalized):
            msg = "keys contains an unsupported PyAutoGUI key name."
            raise DesktopProviderError(msg)
        if len(normalized) == 1:
            self._run_input(lambda: self._pyautogui.press(normalized[0]))
        else:
            self._run_input(lambda: self._pyautogui.hotkey(*normalized))

    def _run_input(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except self._pyautogui.FailSafeException as exc:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg) from exc

    def _screen_size(self) -> tuple[int, int]:
        size = self._pyautogui.size()
        return int(size.width), int(size.height)


__all__ = [
    "DesktopEmergencyStopError",
    "DesktopProvider",
    "DesktopProviderError",
    "PyAutoGuiDesktopProvider",
    "ScreenCapture",
]
