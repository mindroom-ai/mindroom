# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Keep both logo variants small while compression preserves every SVG byte."""

import gzip
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name",
    ["logo", "logo-transparent", "logo-mark", "logo-mark-animated", "logo-animated", "logo-animated-transparent"],
)
def test_export_size_and_lossless_compression(name: str) -> None:
    """Export budgets prevent accidental bulk; SVGZ must decode to the exact SVG."""
    directory = Path(__file__).resolve().parent
    svg = (directory / f"{name}.svg").read_bytes()
    assert len(svg) < 1_600_000
    compressed = (directory / f"{name}.svgz").read_bytes()
    assert len(compressed) < 150_000
    assert gzip.decompress(compressed) == svg
