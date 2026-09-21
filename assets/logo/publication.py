# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Derive app, documentation, and platform icons from the approved artwork."""

from io import BytesIO
from pathlib import Path

from lxml import etree
from PIL import Image
from shading import render


def framed_mark(content: bytes) -> bytes:
    """Remove empty margins without changing any shape, paint, or coordinate."""
    root = etree.fromstring(content)
    with Image.open(BytesIO(render(root))) as image:
        bounds = image.getbbox()
    if bounds is None:
        msg = "Cannot frame an empty logo."
        raise ValueError(msg)
    left, top, right, bottom = bounds
    size = min(1024, max(right - left, bottom - top) + 32)
    left = max(0, min(1024 - size, (left + right - size) // 2))
    top = max(0, min(1024 - size, (top + bottom - size) // 2))
    root.set("viewBox", f"{left} {top} {size} {size}")
    root.set("width", str(size))
    root.set("height", str(size))
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"


def application_outputs(artwork: dict[str, bytes]) -> dict[str, bytes]:
    """Return repository-relative outputs; web consumers use ordinary SVG."""
    mark = artwork["logo-mark.svg"]
    root = etree.fromstring(mark)
    png = {size: render(root, size) for size in (64, 256)}
    favicon = BytesIO()
    with Image.open(BytesIO(png[256])) as image:
        image.save(favicon, format="ICO", sizes=[(size, size) for size in (16, 32, 48, 64, 128, 256)])
    app_icons = {
        appearance: render(etree.fromstring(artwork[f"app-icon-{appearance}.svg"])) for appearance in ("light", "dark")
    }
    menu_bar = etree.parse(str(Path(__file__).with_name("menu-bar.svg"))).getroot()
    return {
        "frontend/public/logo.svg": mark,
        "frontend/public/favicon.png": png[64],
        "docs/assets/logo.svg": mark,
        "docs/assets/favicon.png": png[64],
        "saas-platform/platform-frontend/public/res/branding/mindroom.svg": mark,
        "saas-platform/platform-frontend/src/app/favicon.ico": favicon.getvalue(),
        "avatars/spaces/root_space.png": png[256],
        **{
            f"macos/MindRoom/Sources/MindRoom/Resources/MindRoomMenuBar{suffix}.png": render(menu_bar, size)
            for suffix, size in (("", 18), ("@2x", 36))
        },
        **{
            f"macos/MindRoom/Resources/MindRoom.icon/Assets/{appearance}.png": content
            for appearance, content in app_icons.items()
        },
    }
