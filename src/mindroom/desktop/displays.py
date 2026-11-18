"""Verified logical display bounds and capture pixel scale."""

from __future__ import annotations

import math
from dataclasses import dataclass

from mindroom.desktop.accessibility import DesktopRect


class DisplayMappingError(RuntimeError):
    """The target cannot be mapped to one verified display."""


@dataclass(frozen=True, slots=True)
class DisplayGeometry:
    """One OS display in global logical coordinates with uniform pixel density, which may be unknown."""

    display_id: str
    bounds: DesktopRect
    # Backing pixels along the bounds, or None on both axes when the OS reports no active mode for the display.
    pixel_width: int | None
    pixel_height: int | None

    def __post_init__(self) -> None:
        """Reject malformed or nonuniform transforms before any input."""
        pixel_width, pixel_height = self.pixel_width, self.pixel_height
        dimensions: tuple[int | None, ...] = (self.bounds.width, self.bounds.height)
        if pixel_width is not None or pixel_height is not None:
            dimensions = (*dimensions, pixel_width, pixel_height)
        if any(type(value) is not int or value <= 0 for value in dimensions):
            msg = "Display bounds and scale must have positive integer dimensions."
            raise DisplayMappingError(msg)
        if (
            pixel_width is not None
            and pixel_height is not None
            and not math.isclose(pixel_width / self.bounds.width, pixel_height / self.bounds.height, rel_tol=0.001)
        ):
            msg = "Display scale is inconsistent; reconfigure the display before retrying."
            raise DisplayMappingError(msg)

    @property
    def scale(self) -> float | None:
        """Return pixels per global logical point, or None when the backing scale is unknown; never a guessed 1."""
        return None if self.pixel_width is None else self.pixel_width / self.bounds.width

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
    active = _macos_active_display_ids()
    return tuple(_macos_display(display_id, active=active) for display_id in active)


def macos_main_display() -> DisplayGeometry:
    """Read the main display, the one at global point (0, 0) that the primary-screen target covers."""
    import Quartz  # noqa: PLC0415

    return _macos_display(
        Quartz.CGMainDisplayID(),  # ty: ignore[unresolved-attribute]
        active=_macos_active_display_ids(),
    )


def _macos_active_display_ids() -> tuple[int, ...]:
    import Quartz  # noqa: PLC0415

    error, identifiers, count = Quartz.CGGetActiveDisplayList(32, None, None)  # ty: ignore[unresolved-attribute]
    if error or not identifiers or count != len(identifiers) or count >= 32:
        msg = "macOS display mapping is unavailable; reconnect displays and request fresh app state."
        raise DisplayMappingError(msg)
    return tuple(identifiers)


def _macos_display(display_id: int, *, active: tuple[int, ...]) -> DisplayGeometry:
    """Map one display's active mode onto its logical global bounds, failing closed when the two disagree."""
    import Quartz  # noqa: PLC0415

    bounds = Quartz.CGDisplayBounds(display_id)  # ty: ignore[unresolved-attribute]
    values = (bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)
    if any(not math.isfinite(value) or value != round(value) for value in values):
        msg = "macOS display mapping has uncertain logical bounds."
        raise DisplayMappingError(msg)
    logical = DesktopRect(*(round(value) for value in values))
    # CGDisplayPixelsWide/High count logical points on current macOS; the active mode knows its backing pixels.
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)  # ty: ignore[unresolved-attribute]
    if mode is None:
        # A process can keep reading no mode for a display connected after it started, while the display's ID and
        # logical bounds are current. Input maps through those bounds and captures measure their own pixels, so an
        # active display keeps working with an unknown backing scale; it is never assumed to be 1.
        if display_id not in active:
            msg = "macOS display mode is unavailable; reconnect displays and request fresh app state."
            raise DisplayMappingError(msg)
        return DisplayGeometry(str(display_id), logical, None, None)
    points = (
        int(Quartz.CGDisplayModeGetWidth(mode)),  # ty: ignore[unresolved-attribute]
        int(Quartz.CGDisplayModeGetHeight(mode)),  # ty: ignore[unresolved-attribute]
    )
    pixels = (
        int(Quartz.CGDisplayModeGetPixelWidth(mode)),  # ty: ignore[unresolved-attribute]
        int(Quartz.CGDisplayModeGetPixelHeight(mode)),  # ty: ignore[unresolved-attribute]
    )
    # Bounds of a display rotated by 90 or 270 degrees are rotated, and its mode may read either way round.
    if points == (logical.width, logical.height):
        pixel_width, pixel_height = pixels
    elif points == (logical.height, logical.width):
        pixel_height, pixel_width = pixels
    else:
        msg = "macOS display mode does not match the display's logical bounds; request fresh app state once it settles."
        raise DisplayMappingError(msg)
    return DisplayGeometry(str(display_id), logical, pixel_width, pixel_height)


__all__ = ["DisplayGeometry", "DisplayMappingError", "display_for_region", "macos_displays", "macos_main_display"]
