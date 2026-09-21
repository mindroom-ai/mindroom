# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Keep both logo variants small while compression preserves every SVG byte."""

import gzip
from pathlib import Path

import pytest
from PIL import Image


@pytest.mark.parametrize(
    "name",
    [
        "logo",
        "logo-transparent",
        "logo-mark",
        "logo-mark-animated",
        "logo-animated",
        "logo-animated-transparent",
        "app-icon-light",
        "app-icon-dark",
        "social-preview",
    ],
)
def test_export_size_and_lossless_compression(name: str) -> None:
    """Export budgets prevent accidental bulk; SVGZ must decode to the exact SVG."""
    directory = Path(__file__).resolve().parent
    svg = (directory / f"{name}.svg").read_bytes()
    assert len(svg) < 1_600_000
    compressed = (directory / f"{name}.svgz").read_bytes()
    assert len(compressed) < 150_000
    assert gzip.decompress(compressed) == svg


@pytest.mark.parametrize(("name", "size"), [("social-preview", (1280, 640)), ("github-avatar", (1024, 1024))])
def test_github_upload_images(name: str, size: tuple[int, int]) -> None:
    """GitHub uploads fit the image size limit and remain legible on any page background."""
    path = Path(__file__).resolve().parent / f"{name}.png"
    assert path.stat().st_size < 1_000_000
    with Image.open(path) as image:
        assert image.size == size
        assert image.convert("RGBA").getchannel("A").getextrema() == (255, 255)
