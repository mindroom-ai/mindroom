"""Tests for machine-local desktop geometry and input validation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from mindroom.desktop.provider import PyAutoGuiDesktopProvider

if TYPE_CHECKING:
    import io


class FakeImage:
    """Minimal Pillow-like image exposing capture and resize geometry."""

    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def resize(self, size: tuple[int, int]) -> FakeImage:
        """Return one resized fake image."""
        return FakeImage(size)

    def convert(self, _mode: str) -> FakeImage:
        """Return an RGB-compatible fake image."""
        return self

    def save(self, output: io.BytesIO, **_kwargs: object) -> None:
        """Write deterministic fake JPEG bytes."""
        output.write(b"jpeg")


class FakePyAutoGui:
    """Expose logical screen points separately from Retina capture pixels."""

    @staticmethod
    def size() -> SimpleNamespace:
        """Return logical coordinates accepted by click and scroll."""
        return SimpleNamespace(width=1512, height=982)

    @staticmethod
    def screenshot() -> FakeImage:
        """Return a two-times-density capture."""
        return FakeImage((3024, 1964))


def test_retina_capture_keeps_logical_screen_coordinates() -> None:
    """Screenshots may use dense pixels while actions continue to use logical points."""
    provider = object.__new__(PyAutoGuiDesktopProvider)
    provider._pyautogui = FakePyAutoGui()
    provider._max_screenshot_width = 1600
    provider._jpeg_quality = 80

    capture = provider.screenshot()

    assert (capture.screen_width, capture.screen_height) == (1512, 982)
    assert (capture.image_width, capture.image_height) == (1600, 1039)
