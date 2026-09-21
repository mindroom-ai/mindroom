# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["lxml==5.4.0", "numpy==2.4.4", "pillow==10.4.0", "resvg-py==0.5.0", "scipy==1.17.1"]
# ///
"""Generate the MindRoom logo from shared geometry and reference illumination.

Run from any directory: uv run assets/logo/generate.py
Use --check to verify committed outputs without writing them.
No network service or API key is used to construct the artwork.
"""

import argparse
import gzip
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from math import hypot
from pathlib import Path

import numpy as np
from animation import animated_document
from app_icons import app_icon_document
from artwork import SVG, XLINK, Network, build_document, group, polygon
from geometry import joined_polygons, subtract
from lxml import etree
from optimize import compact_xml, optimize_document
from PIL import Image
from publication import application_outputs, framed_mark
from shading import Colors, color_hex, edge_paint, pixels, render, sample, shade_surfaces

ROOT = Path(__file__).resolve().parent
URL_REFERENCE = re.compile(r"url\(#([^\)]+)\)")


@dataclass
class Highlights:
    """Filled edge facets and their clipped antialias backing at shared corners."""

    network: Network
    paths: dict[str, etree._Element]
    backing: dict[str, etree._Element]


def build_highlights(defs: etree._Element, network: Network) -> Highlights:
    """Join every edge before assigning lighting; never split stroked lines."""
    polygons = joined_polygons(network.points, network.edges, cuts=network.cuts)
    layer = group(
        network.parent,
        network.name,
        "Joined highlight facets: shared miters close every two-, three-, and four-way corner.",
    )
    if network.silhouette is not None:
        boundary_id = f"{network.name}-boundary"
        clip = etree.SubElement(defs, SVG + "clipPath", id=boundary_id)
        etree.SubElement(clip, SVG + "path", d=polygon(network.silhouette))
        layer.set("clip-path", f"url(#{boundary_id})")

    # Adjacent antialiased polygons can expose a hairline even when their
    # coordinates agree. Back only the junctions, clipped to the exact union
    # of the joined facets; this cannot round or enlarge the outer contour.
    clip_id = f"{network.name}-silhouette"
    clip = etree.SubElement(defs, SVG + "clipPath", id=clip_id)
    etree.SubElement(clip, SVG + "path", d=" ".join(polygon(vertices) for vertices in polygons.values()))
    backing_layer = group(
        layer,
        f"{network.name}-backing",
        "Antialias backing stays inside the exact highlight silhouette.",
    )
    backing_layer.set("clip-path", f"url(#{clip_id})")
    incident = defaultdict(list)
    for edge in network.edges:
        for node in (edge.start, edge.end):
            incident[node].append(edge)
    backing = {}
    for node, edges in incident.items():
        if len(edges) < 2:
            continue
        center = network.points[node]
        corners = []
        for edge in edges:
            vertices = polygons[edge.name]
            corners.extend(vertices[1::4] if edge.start == node else vertices[2:5:2])
        radius = max(hypot(*subtract(corner, center)) for corner in corners) + 0.5
        backing[node] = etree.SubElement(
            backing_layer,
            SVG + "circle",
            cx=str(center[0]),
            cy=str(center[1]),
            r=f"{radius:.4f}",
            fill="#000000",
        )
    paths = {}
    for edge in network.edges:
        paths[edge.name] = etree.SubElement(
            layer,
            SVG + "path",
            id=f"{network.name}-{edge.name}",
            d=polygon(polygons[edge.name]),
            fill="#000000",
        )
    return Highlights(network, paths, backing)


def shade_highlights(defs: etree._Element, highlights: Highlights, target: Colors) -> None:
    """Assign sampled gradients to facets and shared endpoint colors to backing."""
    network = highlights.network
    if network.mirrored:
        target = (target + target[:, ::-1]) / 2
    for edge in network.edges:
        paint = edge_paint(defs, f"paint-{network.name}-{edge.name}", network.points, edge, target)
        highlights.paths[edge.name].set("fill", paint)
    for node, circle in highlights.backing.items():
        x, y = network.points[node]
        circle.set("fill", color_hex(sample(target, np.array(x), np.array(y))))


def expand_instances(root: etree._Element, defs: etree._Element) -> None:
    """Give reflected instances independent masks for consistent SVG rendering.

    Some renderers cache masked paint across reflected <use> instances. Cloning
    the paint definitions avoids that cache behavior while source geometry stays
    authored once in artwork.py.
    """
    definitions = {element.get("id"): element for element in root.iter() if element.get("id")}
    for use in list(root.iter(SVG + "use")):
        expand_instance(use, definitions, defs)
    for original in list(defs.findall(SVG + "g")):
        defs.remove(original)


def expand_instance(use: etree._Element, definitions: dict[str, etree._Element], defs: etree._Element) -> None:
    """Expand one instance with a private cache of its cloned paint definitions."""
    instance = use.get("id")
    copied = deepcopy(definitions[use.get(XLINK + "href")[1:]])
    clones = {}

    def clone_definition(name: str) -> str:
        if name in clones:
            return clones[name]
        clone = deepcopy(definitions[name])
        clone_id = f"{name}-{instance}"
        clones[name] = clone_id
        clone.set("id", clone_id)
        for element in clone.iter():
            for key, value in list(element.attrib.items()):
                element.set(key, URL_REFERENCE.sub(lambda match: f"url(#{clone_definition(match[1])})", value))
        defs.append(clone)
        return clone_id

    for element in copied.iter():
        if element.get("id"):
            element.set("id", f"{element.get('id')}-{instance}")
        if use.get("transform"):
            for key, value in list(element.attrib.items()):
                element.set(key, URL_REFERENCE.sub(lambda match: f"url(#{clone_definition(match[1])})", value))
    copied.set("id", instance)
    if use.get("transform"):
        copied.set("transform", use.get("transform"))
    use.getparent().replace(use, copied)


def serialize(root: etree._Element) -> bytes:
    """Compact the SVG only when all rendered RGBA pixels remain identical."""
    optimized = optimize_document(root)
    if not np.array_equal(pixels(render(root)), pixels(render(optimized))):
        msg = "Lossless SVG optimization changed rendered pixels; refusing to export."
        raise ValueError(msg)
    return compact_xml(optimized)


def generate() -> dict[str, bytes]:
    """Produce static and animated SVGs with lossless SVGZ copies."""
    with Image.open(ROOT / "reference.png") as image:
        if image.size != (1024, 1024):
            msg = "reference.png must be 1024 by 1024 pixels"
            raise ValueError(msg)
        target = np.asarray(image.convert("RGB"), dtype=float)
    root, networks = build_document()
    defs = root.find(SVG + "defs")
    faces = list(root.iter(SVG + "path"))
    highlights = [build_highlights(defs, network) for network in networks]
    shade_surfaces(root, defs, faces, target)
    defs.append(etree.Comment(" Generated edge lighting; each gradient paints a closed, joined polygon. "))
    for highlight in highlights:
        shade_highlights(defs, highlight, target)
    expand_instances(root, defs)
    root.remove(defs)
    root.append(
        etree.Comment(" Generated paint definitions. Edit artwork.py for geometry and shading.py for lighting. "),
    )
    root.append(defs)
    outputs = {"logo.svg": serialize(root)}
    transparent = deepcopy(root)
    background = transparent.find(f"{SVG}g[@id='background']")
    transparent.remove(background)
    outputs["logo-transparent.svg"] = serialize(transparent)
    outputs["logo-mark.svg"] = framed_mark(outputs["logo-transparent.svg"])
    for appearance in ("light", "dark"):
        outputs[f"app-icon-{appearance}.svg"] = serialize(app_icon_document(transparent, dark=appearance == "dark"))
    animated = animated_document(root)
    outputs["logo-animated.svg"] = serialize(animated)
    background = animated.find(f"{SVG}g[@id='background']")
    animated.remove(background)
    outputs["logo-animated-transparent.svg"] = serialize(animated)
    outputs["logo-mark-animated.svg"] = framed_mark(outputs["logo-animated-transparent.svg"])
    for name, content in list(outputs.items()):
        if name.endswith(".svg"):
            outputs[name.removesuffix(".svg") + ".svgz"] = gzip.compress(content, mtime=0)
    return outputs


def main() -> None:
    """Regenerate files, or compare the committed artifacts without writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed outputs differ from regenerated artwork",
    )
    args = parser.parse_args()
    artwork = generate()
    repository = ROOT.parents[1]
    outputs = {ROOT / name: content for name, content in artwork.items()}
    outputs.update({repository / name: content for name, content in application_outputs(artwork).items()})
    mismatches = []
    for destination, content in outputs.items():
        name = destination.relative_to(repository).as_posix()
        if args.check:
            matches = destination.exists()
            if matches:
                existing = destination.read_bytes()
                if name.endswith(".png"):
                    matches = np.array_equal(pixels(existing), pixels(content))
                elif name.endswith(".svgz"):
                    matches = gzip.decompress(existing) == gzip.decompress(content)
                else:
                    matches = existing == content
            if not matches:
                mismatches.append(name)
        else:
            destination.write_bytes(content)
    if mismatches:
        parser.exit(1, f"Logo outputs need regeneration: {', '.join(mismatches)}\n")
    names = [path.relative_to(repository).as_posix() for path in outputs]
    print("Logo outputs are current." if args.check else f"Generated {', '.join(names)}.")


if __name__ == "__main__":
    main()
