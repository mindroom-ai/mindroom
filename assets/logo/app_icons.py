# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Compose reproducible dark-glass and light-porcelain app icons around the vector M."""

from copy import deepcopy
from pathlib import Path

from artwork import SVG
from lxml import etree


def app_icon_document(transparent: etree._Element, *, dark: bool) -> etree._Element:
    """Keep the shared logo geometry and add an appearance-specific material treatment."""
    root = deepcopy(transparent)
    defs = root.find(SVG + "defs")
    logo = root.find(f"{SVG}g[@id='logo']")
    frame = logo.find(f"{SVG}g[@id='outer-frame']/{SVG}path")

    background = etree.SubElement(
        defs,
        SVG + "radialGradient",
        id="app-background",
        cx="50%",
        cy="35%",
        r="75%",
    )
    colors = ("#245d72", "#0b2b42", "#031324") if dark else ("#fffefa", "#f7f3ec", "#e7e1d8")
    for offset, color in zip(("0%", "55%", "100%"), colors, strict=True):
        etree.SubElement(background, SVG + "stop", offset=offset, attrib={"stop-color": color})
    root.insert(
        0,
        etree.Element(
            SVG + "rect",
            width="1024",
            height="1024",
            fill="url(#app-background)",
        ),
    )

    edge = etree.SubElement(defs, SVG + "linearGradient", id="app-edge", x2="0", y2="1")
    for offset, color in (("0%", "#9cf8f4"), ("50%", "#36b8ce"), ("100%", "#126180")):
        etree.SubElement(edge, SVG + "stop", offset=offset, attrib={"stop-color": color})
    shadow = etree.SubElement(
        defs,
        SVG + "filter",
        id="app-shadow",
        x="-20%",
        y="-20%",
        width="140%",
        height="140%",
    )
    etree.SubElement(
        shadow,
        SVG + "feDropShadow",
        attrib={
            "dx": "0",
            "dy": "12",
            "stdDeviation": "9",
            "flood-color": "#00101b" if dark else "#796b51",
            "flood-opacity": "0.5" if dark else "0.28",
        },
    )

    # Center the complete M optically, with the same scale in both appearances.
    logo.set("transform", "translate(-15.36 26.84) scale(1.03)")
    silhouette = etree.Element(SVG + "path", d=frame.get("d"), fill="#082b43", filter="url(#app-shadow)")
    logo.insert(0, silhouette)
    if dark:
        apply_glass_material(root, defs, logo, frame)
    return root


def apply_glass_material(
    root: etree._Element,
    defs: etree._Element,
    logo: etree._Element,
    frame: etree._Element,
) -> None:
    """Refract the pane edges, illuminate the legs, and set the M into a glass tile."""
    material = etree.parse(str(Path(__file__).with_name("app-glass-material.svg"))).getroot()
    defs.extend(material.find(SVG + "defs"))
    root.insert(root.index(logo), material.find(SVG + "g"))
    frame.set("fill", "url(#app-glass-frame)")
    for path in list(logo.iter(SVG + "path")):
        name = path.get("id", "")
        if name.startswith(("outer-glass", "roof-glass", "foot-outer", "foot-inner")):
            path.set("filter", "url(#app-glass-pane)")
        if name.startswith(("outer-glass", "foot-outer")):
            # Keep the glow in the same mirrored parent as its face.
            glow = "app-glass-leg-glow" if name.startswith("outer-glass") else "app-glass-foot-glow"
            path.addnext(etree.Element(SVG + "path", d=path.get("d"), fill=f"url(#{glow})"))
    etree.SubElement(
        logo,
        SVG + "path",
        d=frame.get("d"),
        fill="none",
        attrib={"stroke": "url(#app-edge)", "stroke-width": "1.1", "opacity": "0.7"},
    )
