"""Logical display geometry is independent of capture pixel density."""

import sys
from types import SimpleNamespace

import pytest

from mindroom.desktop.accessibility import DesktopRect
from mindroom.desktop.displays import DisplayGeometry, DisplayMappingError, display_for_region, macos_displays


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
    bounds = {
        1: SimpleNamespace(origin=SimpleNamespace(x=0, y=0), size=SimpleNamespace(width=1000, height=800)),
        2: SimpleNamespace(origin=SimpleNamespace(x=-1920, y=-200), size=SimpleNamespace(width=1920, height=1080)),
    }
    quartz = SimpleNamespace(
        CGGetActiveDisplayList=lambda _limit, _ids, _count: (0, (1, 2), 2),
        CGDisplayBounds=bounds.__getitem__,
        CGDisplayPixelsWide=lambda display: 1000 if display == 1 else 3840,
        CGDisplayPixelsHigh=lambda display: 800 if display == 1 else 2160,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    displays = macos_displays()
    assert displays[1].bounds == DesktopRect(-1920, -200, 1920, 1080)
    assert displays[1].scale == 2.0
