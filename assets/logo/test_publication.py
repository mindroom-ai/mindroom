# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Published icons must retain the complete approved M and its colors."""

from pathlib import Path

import numpy as np
import pytest
from lxml import etree
from shading import pixels, render

ROOT = Path(__file__).resolve().parent


def test_transparent_svg_preserves_opaque_artwork_pixels() -> None:
    """Removing the background preserves the M's opaque colors."""
    transparent = pixels(render(etree.parse(str(ROOT / "logo-transparent.svg")).getroot()))
    background = pixels(render(etree.parse(str(ROOT / "logo.svg")).getroot()))
    opaque = transparent[:, :, 3] == 255
    assert opaque.any()
    assert (transparent[:, :, 3] == 0).any()
    assert np.array_equal(transparent[opaque, :3], background[opaque, :3])


@pytest.mark.parametrize("name", ["logo-mark", "logo-mark-animated"])
def test_framed_mark_only_removes_empty_canvas(name: str) -> None:
    """The cropped mark keeps coverage, and restoring its viewport is exact."""
    path = ROOT / f"{name}.svg"
    assert path.is_file(), "Export a tightly framed mark for small app icons."
    mark = etree.parse(str(path)).getroot()
    left, top, width, height = map(int, mark.get("viewBox").split())
    assert width == height < 1024
    assert 0 <= left < left + width <= 1024
    assert 0 <= top < top + height <= 1024
    framed = pixels(render(mark, width))
    original = pixels(render(etree.parse(str(ROOT / "logo-transparent.svg")).getroot()))
    coverage = np.zeros((1024, 1024), dtype=np.uint8)
    coverage[top : top + height, left : left + width] = framed[:, :, 3]
    assert np.array_equal(coverage, original[:, :, 3])
    # Compare colors in the same coordinate system. Changing the viewport
    # origin can shift floating-point rasterization by one channel level.
    mark.set("viewBox", "0 0 1024 1024")
    mark.set("width", "1024")
    mark.set("height", "1024")
    assert np.array_equal(pixels(render(mark)), original)
