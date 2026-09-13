# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Published icons must retain the complete approved M and its colors."""

from pathlib import Path

import numpy as np
from lxml import etree
from shading import pixels, render

ROOT = Path(__file__).resolve().parent


def test_transparent_png_preserves_the_full_m() -> None:
    """Raster fallback matches the SVG and keeps every opaque artwork pixel."""
    path = ROOT / "logo-transparent.png"
    assert path.is_file(), "Export a transparent PNG for image viewers."
    transparent = pixels(path.read_bytes())
    svg = etree.parse(str(ROOT / "logo-transparent.svg")).getroot()
    assert np.array_equal(transparent, pixels(render(svg)))
    background = pixels((ROOT / "preview.png").read_bytes())
    opaque = transparent[:, :, 3] == 255
    assert opaque.any()
    assert (transparent[:, :, 3] == 0).any()
    assert np.array_equal(transparent[opaque, :3], background[opaque, :3])


def test_framed_mark_only_removes_empty_canvas() -> None:
    """The cropped mark keeps coverage, and restoring its viewport is exact."""
    path = ROOT / "logo-mark.svg"
    assert path.is_file(), "Export a tightly framed mark for small app icons."
    mark = etree.parse(str(path)).getroot()
    left, top, width, height = map(int, mark.get("viewBox").split())
    assert width == height < 1024
    assert 0 <= left < left + width <= 1024
    assert 0 <= top < top + height <= 1024
    framed = pixels(render(mark, width))
    original = pixels((ROOT / "logo-transparent.png").read_bytes())
    coverage = np.zeros((1024, 1024), dtype=np.uint8)
    coverage[top : top + height, left : left + width] = framed[:, :, 3]
    assert np.array_equal(coverage, original[:, :, 3])
    # Compare colors in the same coordinate system. Changing the viewport
    # origin can shift floating-point rasterization by one channel level.
    mark.set("viewBox", "0 0 1024 1024")
    mark.set("width", "1024")
    mark.set("height", "1024")
    assert np.array_equal(pixels(render(mark)), original)
