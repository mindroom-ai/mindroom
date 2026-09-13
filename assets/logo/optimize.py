# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Compact generated SVG data without approximating colors or coordinates.

Keep instance-specific masks and patterns: sharing those can trigger paint
cache differences in renderers. Only identical gradients and stops are shared.
"""

import re
from collections import defaultdict
from collections.abc import Iterator
from copy import deepcopy

from artwork import SVG, XLINK
from lxml import etree

PAINT_REFERENCE = re.compile(r"url\(#([^\)]+)\)")
SCALAR_ATTRIBUTES = {
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "width",
    "height",
    "offset",
    "r",
    "cx",
    "cy",
    "stroke-width",
    "stop-opacity",
}


def rewrite_references(root: etree._Element, aliases: dict[str, str]) -> None:
    """Redirect paint URLs and gradient template links to equivalent IDs."""
    for element in root.iter():
        for key, value in list(element.attrib.items()):
            if key in {"href", XLINK + "href"} and value.startswith("#"):
                element.set(key, "#" + aliases.get(value[1:], value[1:]))
            else:
                element.set(key, PAINT_REFERENCE.sub(lambda match: f"url(#{aliases.get(match[1], match[1])})", value))
        if element.tag == SVG + "style" and element.text:
            element.text = PAINT_REFERENCE.sub(lambda match: f"url(#{aliases.get(match[1], match[1])})", element.text)


def references(root: etree._Element) -> Iterator[str]:
    """Find generated paint dependencies, including transitive template links."""
    for element in root.iter():
        for key, value in element.attrib.items():
            yield from PAINT_REFERENCE.findall(value)
            if key in {"href", XLINK + "href"} and value.startswith("#"):
                yield value[1:]
        if element.tag == SVG + "style" and element.text:
            yield from PAINT_REFERENCE.findall(element.text)


def remove_unused(root: etree._Element, defs: etree._Element) -> None:
    """Discard unreachable paints, including the transparent export's backdrop."""
    definitions = {element.get("id"): element for element in defs if element.get("id")}
    pending = [name for child in root if child is not defs for name in references(child)]
    used = set()
    while pending:
        name = pending.pop()
        if name not in used and name in definitions:
            used.add(name)
            pending.extend(references(definitions[name]))
    for name, element in definitions.items():
        if name not in used:
            defs.remove(element)


def signature(element: etree._Element) -> bytes:
    """Compare complete gradient content and attributes, excluding its name."""
    copied = deepcopy(element)
    copied.attrib.pop("id", None)
    return etree.tostring(copied, method="c14n")


def share_stops(defs: etree._Element) -> None:
    """Use stop-only templates so all original gradient coordinates survive."""
    sequences = defaultdict(list)
    for gradient in defs.findall(SVG + "linearGradient"):
        if not len(gradient):
            continue
        sequences[tuple(signature(stop) for stop in gradient)].append(gradient)
    for gradients in sequences.values():
        if len(gradients) < 2:
            continue
        template = etree.SubElement(defs, SVG + "linearGradient", id=f"stops{len(defs)}")
        template.extend(gradients[0][:])
        for gradient in gradients:
            gradient[:] = []
            gradient.set(XLINK + "href", "#" + template.get("id"))


def deduplicate_gradients(root: etree._Element, defs: etree._Element) -> None:
    """Share identical unmasked paints while retaining every original stop."""
    seen, aliases = {}, {}
    for gradient in list(defs):
        if gradient.tag not in {SVG + "linearGradient", SVG + "radialGradient"}:
            continue
        key = signature(gradient)
        if key in seen:
            aliases[gradient.get("id")] = seen[key]
            defs.remove(gradient)
        else:
            seen[key] = gradient.get("id")
    rewrite_references(root, aliases)


def optimize_document(source: etree._Element) -> etree._Element:
    """Return a smaller, equivalent copy of this generator's SVG document."""
    root = deepcopy(source)
    defs = root.find(SVG + "defs")
    deduplicate_gradients(root, defs)
    remove_unused(root, defs)
    share_stops(defs)

    # Keep named geometry and surface paints readable. Only the numerous
    # generated row, band, fade, and mask identifiers receive short names.
    aliases = {}
    for element in defs:
        name = element.get("id") or ""
        if any(part in name for part in ("-row-", "-fade-", "-mask-", "-band-")):
            aliases[name] = f"p{len(aliases)}"
    for element in root.iter():
        if element.get("id") in aliases:
            element.set("id", aliases[element.get("id")])
    rewrite_references(root, aliases)

    # Dropping trailing zeros leaves the numeric value exact; no precision
    # rounding, color quantization, curve fitting, or geometry edits occur.
    for element in root.iter():
        for key, value in list(element.attrib.items()):
            if key in SCALAR_ATTRIBUTES and re.fullmatch(r"-?\d+\.\d+", value):
                element.set(key, value.rstrip("0").rstrip("."))
    return root


def compact_xml(root: etree._Element) -> bytes:
    """Indent the authored layers and keep each generated definition on a line."""
    etree.indent(root, space="  ")
    defs = root.find(SVG + "defs")
    for definition in defs:
        for element in definition.iter():
            if element.text is not None and not element.text.strip():
                element.text = None
            element.tail = None
        definition.tail = "\n    "
    if len(defs):
        defs[-1].tail = "\n  "
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"
