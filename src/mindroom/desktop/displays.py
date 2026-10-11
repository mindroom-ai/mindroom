"""Verified logical display bounds and capture pixel scale."""

from __future__ import annotations

import math
from dataclasses import dataclass

from mindroom.desktop.accessibility import DesktopRect


class DisplayMappingError(RuntimeError):
    """The target cannot be mapped to one verified display."""


@dataclass(frozen=True, slots=True)
class DisplayGeometry:
    """One OS display in global logical coordinates with uniform pixel density."""

    display_id: str
    bounds: DesktopRect
    pixel_width: int
    pixel_height: int

    def __post_init__(self) -> None:
        """Reject malformed or nonuniform transforms before any input."""
        dimensions = (self.bounds.width, self.bounds.height, self.pixel_width, self.pixel_height)
        if any(type(value) is not int or value <= 0 for value in dimensions):
            msg = "Display bounds and scale must have positive integer dimensions."
            raise DisplayMappingError(msg)
        if not math.isclose(
            self.pixel_width / self.bounds.width,
            self.pixel_height / self.bounds.height,
            rel_tol=0.001,
        ):
            msg = "Display scale is inconsistent; reconfigure the display before retrying."
            raise DisplayMappingError(msg)

    @property
    def scale(self) -> float:
        """Return pixels per global logical point."""
        return self.pixel_width / self.bounds.width

    def to_result(self) -> dict[str, object]:
        """Describe the display without exposing other applications."""
        return {"id": self.display_id, "bounds": self.bounds.to_result(), "scale": self.scale}


def display_for_region(displays: tuple[DisplayGeometry, ...], region: DesktopRect) -> DisplayGeometry:
    """Require the whole target to lie on one unambiguous display."""
    matches = [display for display in displays if _contains(display.bounds, region)]
    intersecting = [display for display in displays if _intersects(display.bounds, region)]
    if len(matches) != 1 or len(intersecting) != 1:
        msg = "Place the full application window inside one display and request fresh app state."
        raise DisplayMappingError(msg)
    return matches[0]


def _contains(outer: DesktopRect, inner: DesktopRect) -> bool:
    return (
        inner.width > 0
        and inner.height > 0
        and inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def _intersects(first: DesktopRect, second: DesktopRect) -> bool:
    return (
        first.x < second.x + second.width
        and second.x < first.x + first.width
        and first.y < second.y + second.height
        and second.y < first.y + first.height
    )


def macos_displays() -> tuple[DisplayGeometry, ...]:
    """Read the active Quartz display topology without guessing unavailable geometry."""
    import Quartz  # noqa: PLC0415

    error, identifiers, count = Quartz.CGGetActiveDisplayList(32, None, None)  # ty: ignore[unresolved-attribute]
    if error or not identifiers or count != len(identifiers) or count >= 32:
        msg = "macOS display mapping is unavailable; reconnect displays and request fresh app state."
        raise DisplayMappingError(msg)
    displays = []
    for display_id in identifiers:
        bounds = Quartz.CGDisplayBounds(display_id)  # ty: ignore[unresolved-attribute]
        values = (bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)
        if any(not math.isfinite(value) or value != round(value) for value in values):
            msg = "macOS display mapping has uncertain logical bounds."
            raise DisplayMappingError(msg)
        displays.append(
            DisplayGeometry(
                str(display_id),
                DesktopRect(*(round(value) for value in values)),
                int(Quartz.CGDisplayPixelsWide(display_id)),  # ty: ignore[unresolved-attribute]
                int(Quartz.CGDisplayPixelsHigh(display_id)),  # ty: ignore[unresolved-attribute]
            ),
        )
    return tuple(displays)


__all__ = ["DisplayGeometry", "DisplayMappingError", "display_for_region", "macos_displays"]
