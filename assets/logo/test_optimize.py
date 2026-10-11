# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Protect gradient details that affect browser rasterization."""

from artwork import SVG, XLINK
from lxml import etree
from optimize import optimize_document


def test_repeated_stops_survive_shared_templates() -> None:
    """Even constant-color stops must survive: removing them changed Chromium pixels."""
    root = etree.Element(SVG + "svg", nsmap={None: SVG[1:-1], "xlink": XLINK[1:-1]})
    defs = etree.SubElement(root, SVG + "defs")
    expected = [("0", "#112233"), ("0.25", "#112233"), ("0.5", "#112233"), ("1", "#aabbcc")]
    for index in range(2):
        name = f"paint-sample-row-{index}"
        gradient = etree.SubElement(defs, SVG + "linearGradient", id=name, x1=str(index), x2="100")
        for offset, color in expected:
            etree.SubElement(gradient, SVG + "stop", offset=offset, attrib={"stop-color": color})
        etree.SubElement(root, SVG + "rect", id=f"face-{index}", width="100", height="100", fill=f"url(#{name})")
    source = etree.tostring(root)
    optimized = optimize_document(root)
    assert etree.tostring(root) == source
    definitions = {node.get("id"): node for node in optimized.find(SVG + "defs")}
    for face in optimized.findall(SVG + "rect"):
        gradient = definitions[face.get("fill").removeprefix("url(#").removesuffix(")")]
        assert gradient.get(XLINK + "href"), "Fixture must exercise inherited stops."
        template = definitions[gradient.get(XLINK + "href")[1:]]
        assert [(stop.get("offset"), stop.get("stop-color")) for stop in template] == expected
