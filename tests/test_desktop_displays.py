"""Logical display geometry is independent of capture pixel density."""

import sys
from types import SimpleNamespace

import pytest

from mindroom.desktop.accessibility import DesktopRect
from mindroom.desktop.displays import (
    DisplayGeometry,
    DisplayMappingError,
    display_for_region,
    macos_displays,
    macos_main_display,
)


def test_negative_origin_retina_display_contains_exact_app_region() -> None:
    """A secondary screen to the left retains negative logical points and 2x scale."""
    secondary = DisplayGeometry("2", DesktopRect(-1920, 0, 1920, 1080), 3840, 2160)
    primary = DisplayGeometry("1", DesktopRect(0, 0, 1512, 982), 3024, 1964)
    selected = display_for_region((primary, secondary), DesktopRect(-1800, 100, 800, 600))
    assert selected.display_id == "2"
    assert selected.scale == 2.0
    assert selected.to_result() == {
        "id": "2",
        "bounds": {"x": -1920, "y": 0, "width": 1920, "height": 1080},
        "scale": 2.0,
    }


@pytest.mark.parametrize("region", [DesktopRect(-10, 20, 100, 100), DesktopRect(1100, 0, 100, 100)])
def test_window_spanning_or_outside_displays_fails_closed(region: DesktopRect) -> None:
    """An unsupported capture mapping cannot silently clip or choose a monitor."""
    displays = (
        DisplayGeometry("left", DesktopRect(-1000, 0, 1000, 800), 1000, 800),
        DisplayGeometry("right", DesktopRect(0, 0, 1000, 800), 2000, 1600),
    )
    with pytest.raises(DisplayMappingError, match="one display"):
        display_for_region(displays, region)


def test_mirrored_geometry_is_ambiguous() -> None:
    """Two displays at the same logical location do not provide exact identity."""
    displays = tuple(DisplayGeometry(name, DesktopRect(0, 0, 1000, 800), 1000, 800) for name in ("a", "b"))
    with pytest.raises(DisplayMappingError, match="one display"):
        display_for_region(displays, DesktopRect(10, 10, 200, 200))


def test_inconsistent_axis_scale_is_rejected() -> None:
    """Nonuniform or unknown transforms cannot be used for input or capture metadata."""
    with pytest.raises(DisplayMappingError, match="scale"):
        DisplayGeometry("bad", DesktopRect(0, 0, 1000, 800), 2000, 800)


def test_macos_display_inventory_uses_global_bounds_and_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS enumeration preserves secondary negative origins and per-display density."""
    quartz = _display_quartz(
        {1: (0, 0, 1000, 800), 2: (-1920, -200, 1920, 1080)},
        {1: (1000, 800, 1000, 800), 2: (1920, 1080, 3840, 2160)},
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    displays = macos_displays()
    assert displays[1].bounds == DesktopRect(-1920, -200, 1920, 1080)
    assert displays[1].scale == 2.0


def _display_quartz(
    bounds: dict[int, tuple[float, float, float, float]],
    modes: dict[int, tuple[int, int, int, int] | None],
    *,
    active: tuple[int, ...] | None = None,
) -> SimpleNamespace:
    """Fake Quartz: `bounds` are logical (x, y, width, height); `modes` are (points wide, high, pixels wide, high).

    CGDisplayPixelsWide/High report logical points, as measured on macOS 26 (2026-11-17), so they cannot give the scale.
    `active` lists the active display IDs, every display in `bounds` by default.
    """
    active_ids = tuple(bounds) if active is None else active
    return SimpleNamespace(
        CGGetActiveDisplayList=lambda _limit, _ids, _count: (0, active_ids, len(active_ids)),
        CGDisplayBounds=lambda display: SimpleNamespace(
            origin=SimpleNamespace(x=bounds[display][0], y=bounds[display][1]),
            size=SimpleNamespace(width=bounds[display][2], height=bounds[display][3]),
        ),
        CGDisplayPixelsWide=lambda display: bounds[display][2],
        CGDisplayPixelsHigh=lambda display: bounds[display][3],
        CGDisplayCopyDisplayMode=lambda display: None if modes[display] is None else ("mode", display),
        CGDisplayModeGetWidth=lambda mode: modes[mode[1]][0],
        CGDisplayModeGetHeight=lambda mode: modes[mode[1]][1],
        CGDisplayModeGetPixelWidth=lambda mode: modes[mode[1]][2],
        CGDisplayModeGetPixelHeight=lambda mode: modes[mode[1]][3],
    )


def test_macos_display_scale_comes_from_the_active_mode_not_logical_pixel_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported scale is the active mode's backing pixels per logical point.

    Measured live (2026-11-17): display 4 had bounds and mode 2560x1440 points over 5120x2880 pixels, yet MindRoom
    reported scale 1.0 while its screenshots captured at scale 2, because CGDisplayPixelsWide/High are logical.
    """
    monkeypatch.setitem(sys.modules, "Quartz", _display_quartz({4: (0, 0, 2560, 1440)}, {4: (2560, 1440, 5120, 2880)}))

    (display,) = macos_displays()

    assert display.bounds == DesktopRect(0, 0, 2560, 1440)
    assert (display.pixel_width, display.pixel_height) == (5120, 2880)
    assert display.to_result() == {"id": "4", "bounds": {"x": 0, "y": 0, "width": 2560, "height": 1440}, "scale": 2.0}


@pytest.mark.parametrize(
    "mode",
    [(2560, 1440, 5120, 2880), (1440, 2560, 2880, 5120)],
    ids=["native mode", "rotated mode"],
)
def test_macos_rotated_display_keeps_logical_bounds_and_mode_scale(
    monkeypatch: pytest.MonkeyPatch,
    mode: tuple[int, int, int, int],
) -> None:
    """A portrait display rotated 90 degrees maps its mode's pixels onto its rotated bounds whichever way the mode reads."""
    monkeypatch.setitem(sys.modules, "Quartz", _display_quartz({2: (2560, 0, 1440, 2560)}, {2: mode}))

    (display,) = macos_displays()

    assert display.bounds == DesktopRect(2560, 0, 1440, 2560)
    assert (display.pixel_width, display.pixel_height) == (2880, 5120)
    assert display.scale == 2.0


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ((1920, 1080, 3840, 2160), "does not match"),
        ((2560, 1440, 5120, 1440), "scale"),
        ((2560, 1440, 0, 0), "positive"),
    ],
    ids=["other size", "nonuniform", "no pixels"],
)
def test_macos_display_mapping_fails_closed_without_a_matching_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: tuple[int, int, int, int],
    error: str,
) -> None:
    """A mode that does not cover the logical bounds uniformly is wrong, not unknown, so nothing is mapped."""
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        _display_quartz({1: (0, 0, 1512, 982), 4: (0, 0, 2560, 1440)}, {1: (1512, 982, 3024, 1964), 4: mode}),
    )

    with pytest.raises(DisplayMappingError, match=error):
        macos_displays()


def test_macos_main_display_reads_the_display_at_the_global_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The primary-screen target covers whichever display is main now, with that display's own ID and mode scale."""
    quartz = _display_quartz(
        {1: (-1512, 0, 1512, 982), 9: (0, 0, 2560, 1440)},
        {1: (1512, 982, 3024, 1964), 9: (2560, 1440, 5120, 2880)},
    )
    quartz.CGMainDisplayID = lambda: 9
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    assert macos_main_display() == DisplayGeometry("9", DesktopRect(0, 0, 2560, 1440), 5120, 2880)


def test_macos_active_display_without_a_mode_has_unknown_scale_never_1x(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process can keep reading no mode for a display connected after it started, while its ID and bounds are current.

    Measured live (2026-11-17): Quartz reported the new main display's ID and logical bounds, but its mode stayed
    missing in that process, so fresh observations and status failed. Input maps through the verified logical bounds and
    captures measure their own pixels, so the backing scale is reported as unknown instead.
    """
    quartz = _display_quartz(
        {4: (-2560, 0, 2560, 1440), 9: (0, 0, 2560, 1440)},
        {4: (2560, 1440, 5120, 2880), 9: None},
    )
    quartz.CGMainDisplayID = lambda: 9
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    known, unknown = macos_displays()

    assert known.scale == 2.0
    assert unknown == DisplayGeometry("9", DesktopRect(0, 0, 2560, 1440), None, None)
    assert unknown.to_result() == {"id": "9", "bounds": {"x": 0, "y": 0, "width": 2560, "height": 1440}, "scale": None}
    assert macos_main_display() == unknown
    assert display_for_region((known, unknown), DesktopRect(100, 100, 800, 600)) == unknown


@pytest.mark.parametrize(
    ("bounds", "active", "error"),
    [
        ((0, 0, 2560, 1440), (4,), "display mode is unavailable"),
        ((0, 0, 0, 0), (4, 9), "positive"),
        ((0, 0, 2560.5, 1440), (4, 9), "uncertain logical bounds"),
    ],
    ids=["inactive display", "zero bounds", "fractional bounds"],
)
def test_macos_display_without_a_mode_is_refused_unless_active_with_whole_positive_bounds(
    monkeypatch: pytest.MonkeyPatch,
    bounds: tuple[float, float, float, float],
    active: tuple[int, ...],
    error: str,
) -> None:
    """Only an active display with whole, positive logical bounds may lack a mode; others stay refused."""
    quartz = _display_quartz(
        {4: (-2560, 0, 2560, 1440), 9: bounds},
        {4: (2560, 1440, 5120, 2880), 9: None},
        active=active,
    )
    quartz.CGMainDisplayID = lambda: 9
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    with pytest.raises(DisplayMappingError, match=error):
        macos_main_display()


@pytest.mark.parametrize("pixels", [(5120, None), (None, 2880)], ids=["no height", "no width"])
def test_display_scale_is_unknown_on_both_axes_or_neither(pixels: tuple[int | None, int | None]) -> None:
    """Half-known pixels are malformed, not unknown."""
    with pytest.raises(DisplayMappingError, match="positive"):
        DisplayGeometry("9", DesktopRect(0, 0, 2560, 1440), *pixels)
