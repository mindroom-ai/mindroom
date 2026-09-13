# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.

"""Named vertices and glass faces for the MindRoom logo.

Edit corner coordinates here. Faces and highlight edges refer to the same
vertices, so moving a corner cannot separate its two or three incident edges.
The right wing and cube face mirror the authored left-hand geometry.
"""

from dataclasses import dataclass
from itertools import pairwise

from geometry import Edge, Point, intersection, subtract
from lxml import etree

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"


@dataclass
class Network:
    """A connected family of highlight edges painted in one SVG group."""

    name: str
    parent: etree._Element
    points: dict[str, Point]
    edges: list[Edge]
    cuts: dict[str, Point]
    mirrored: bool = False
    silhouette: list[Point] | None = None


def polygon(points: list[Point]) -> str:
    """Serialize a closed polygon with stable, readable coordinates."""
    return "M " + " L ".join(f"{x:.4f},{y:.4f}" for x, y in points) + " Z"


def chain(name: str, vertices: str, width: float) -> list[Edge]:
    """Define an edge chain, including explicit vertices at every T junction."""
    names = vertices.split()
    return [Edge(f"{name}-{i + 1}", a, b, width) for i, (a, b) in enumerate(pairwise(names))]


def face(parent: etree._Element, name: str, points: dict[str, Point], vertices: str, color: str) -> None:
    """Emit an editable face whose corners come from the shared vertex table."""
    etree.SubElement(parent, SVG + "path", id=name, d=polygon([points[key] for key in vertices.split()]), fill=color)


def group(parent: etree._Element, name: str, comment: str) -> etree._Element:
    """Create a named, documented layer."""
    parent.append(etree.Comment(f" {comment} "))
    return etree.SubElement(parent, SVG + "g", id=name)


def room_networks(defs: etree._Element) -> list[Network]:
    """Author the upper room, tall pane, and lower glass block on one side."""
    # Upper and lower room corners. Intermediate rim points are real T joins,
    # not independent line endpoints placed approximately over the rim.
    wing = {
        "roof-left": (189.0, 233.0),
        "roof-apex": (311.0, 157.0),
        "roof-back": (497.0, 276.0),
        "roof-front": (376.0, 350.0),
        "roof-crease": (311.0, 307.0),
        "tower-top": (288.0, 295.0),
        "tower-foot": (288.0, 638.0),
        "tower-left-foot": (189.0, 575.0),
        "well-left": (314.0, 295.0),
        "well-apex": (403.0, 237.5),
        "well-right": (481.5, 285.5),
        "well-bottom": (403.0, 344.0),
        "well-face-left": (317.0, 295.0),
        "well-face-right": (484.0, 287.0),
        "well-face-front": (397.0, 343.0),
        "inner-top": (288.0, 297.0),
        "inner-right-top": (405.0, 365.0),
        "inner-right-bottom": (405.0, 578.0),
        "beam-left": (288.0, 320.0),
        "beam-right": (358.0, 364.0),
        "mullion-top-right": (306.0, 331.0),
        "mullion-bottom-right": (306.0, 626.0),
        "gold-top": (308.5, 320 + (308.5 - 288) * 44 / 70),
        "gold-foot": (308.5, 638 - (308.5 - 288) * 2 / 3),
        "mullion-top": (290.0, 320 + 2 * 44 / 70),
        "mullion-foot": (290.0, 638 - 2 * 2 / 3),
        "foot-outer-top": (189.0, 604.0),
        "foot-front-top": (287.5, 660.0),
        "foot-inner-top": (405.0, 583.0),
        "foot-outer-bottom": (189.0, 729.0),
        "foot-front-bottom": (287.5, 785.0),
        "foot-inner-bottom": (405.0, 709.0),
        "foot-back-left": (208.0, 591.0),
        "foot-back-right": (393.0, 568.0),
        "foot-cap-tip": (405.0, 576.0),
        "floor-back": (387.0, 697.0),
        "floor-front": (287.5, 763.3),
        "floor-fade-end": (303.0, 753.0),
    }
    frame_a, frame_b = (358.0, 361.0), (512.0, 265.0)
    frame_direction = subtract(frame_b, frame_a)
    for name, start in [("roof-back", "roof-apex"), ("roof-front", "roof-left"), ("well-right", "well-apex")]:
        wing[name] = intersection(wing[start], subtract(wing[name], wing[start]), frame_a, frame_direction)
    wing["well-bottom"] = intersection(wing["well-apex"], (0.0, 1.0), frame_a, frame_direction)
    rim_direction = subtract(wing["foot-inner-top"], wing["foot-front-top"])
    wing["floor-top"] = intersection(wing["floor-back"], (0.0, 1.0), wing["foot-front-top"], rim_direction)
    wing["floor-diagonal-front"] = intersection(
        (306.0, 646.0),
        subtract(wing["floor-back"], (306.0, 646.0)),
        wing["foot-front-top"],
        rim_direction,
    )

    rooms = group(defs, "room-wing", "One authored room wing; its mirrored instance shares the same geometry.")
    roof = group(rooms, "upper-room-glass", "Roof glass and recessed room. Highlights are joined separately below.")
    face(roof, "roof-glass", wing, "roof-left roof-apex roof-back roof-front", "#6bbeb7")
    face(roof, "roof-left-plane", wing, "roof-left roof-apex roof-crease tower-top", "#31899b")
    face(roof, "roof-well", wing, "well-face-left well-apex well-face-right well-face-front", "#617878")
    tall = group(rooms, "tall-room-glass", "Tall room faces, inset mullion, and the navy beam above them.")
    face(tall, "inner-glass", wing, "inner-top inner-right-top inner-right-bottom tower-foot", "#7b8c7c")
    face(tall, "inner-mullion", wing, "beam-left mullion-top-right mullion-bottom-right tower-foot", "#9ca58a")
    face(tall, "upper-beam", wing, "tower-top roof-front beam-right beam-left", "#0b324c")
    face(tall, "outer-glass", wing, "roof-left tower-top tower-foot tower-left-foot", "#155773")
    lower = group(rooms, "lower-room-glass", "Lower glass faces and cap use the exact same rim and perimeter vertices.")
    face(lower, "foot-outer", wing, "foot-outer-top foot-front-top foot-front-bottom foot-outer-bottom", "#17677e")
    face(lower, "foot-inner", wing, "foot-front-top foot-inner-top foot-inner-bottom foot-front-bottom", "#4c7f83")
    face(lower, "foot-floor", wing, "floor-front floor-back foot-inner-bottom foot-front-bottom", "#315d71")
    face(
        lower,
        "foot-cap",
        wing,
        "foot-outer-top foot-back-left tower-foot foot-back-right foot-cap-tip foot-inner-top foot-front-top",
        "#87bba8",
    )

    wing_edges = [
        *chain("roof-rim", "roof-left roof-apex roof-back", 1.4),
        Edge("roof-front-rim", "roof-left", "roof-front", 3.2),
        *chain("tower-border", "roof-left tower-left-foot tower-foot", 1.2),
        *chain("well-rim", "well-left well-apex well-right", 5),
        Edge("well-vertical", "well-apex", "well-bottom", 4.5),
        Edge("tower-gold", "gold-top", "gold-foot", 4),
        Edge("tower-mullion", "mullion-top", "mullion-foot", 2),
        *chain("cap-back", "tower-foot mullion-foot gold-foot foot-back-right", 1.2),
        *chain("foot-rim", "foot-outer-top foot-front-top floor-diagonal-front floor-top foot-inner-top", 4),
        Edge("foot-front", "foot-front-top", "foot-front-bottom", 5),
        *chain(
            "foot-outline",
            "foot-outer-top foot-outer-bottom foot-front-bottom foot-inner-bottom foot-inner-top",
            1.4,
        ),
        Edge("floor-vertical", "floor-top", "floor-back", 1.8),
        Edge("floor-gold", "floor-diagonal-front", "floor-back", 3.4),
        Edge("floor-short", "floor-back", "foot-inner-bottom", 1.15),
        Edge("floor-fading", "floor-back", "floor-fade-end", 1.15, 0.55),
    ]
    wing_cuts = {
        "well-left": (1.0, 0.0),
        "roof-back": frame_direction,
        "roof-front": frame_direction,
        "well-right": frame_direction,
        "well-bottom": frame_direction,
        "gold-top": (70.0, 44.0),
        "mullion-top": (70.0, 44.0),
    }
    # Miters on the lower block stop at the actual cap/side silhouette. This
    # keeps a narrow outside border from pulling a wide rim into the frame.
    foot_edges = [edge for edge in wing_edges if edge.name.startswith(("foot-", "floor-"))]
    room_edges = [edge for edge in wing_edges if edge not in foot_edges]
    foot_boundary = "foot-outer-top foot-back-left tower-foot foot-back-right foot-cap-tip foot-inner-bottom foot-front-bottom foot-outer-bottom"
    return [
        Network("room-highlights", rooms, wing, room_edges, wing_cuts, True),
        Network("foot-highlights", rooms, wing, foot_edges, {}, True, [wing[name] for name in foot_boundary.split()]),
    ]


def cube_face_network(defs: etree._Element) -> Network:
    """Author one central side panel and its four-way interior junction."""
    # The central side panel has a four-way gold junction. Its vertical, two
    # diagonals, and floor meet at an exact intersection, not nearby guesses.
    cube = {
        "frame-top": (358.0, 361.0),
        "frame-front": (512.0, 455.0),
        "frame-bottom": (512.0, 643.0),
        "frame-left-bottom": (358.0, 546.0),
        "a": (382.5, 403.0),
        "b": (487.0, 470.0),
        "c": (487.0, 598.0),
        "d": (382.5, 533.0),
        "floor-right": (487.0, 574.0),
    }
    cube["back"] = intersection((402.5, 0.0), (0.0, 1.0), cube["d"], subtract(cube["b"], cube["d"]))
    cube["back-top"] = intersection((402.5, 0.0), (0.0, 1.0), cube["a"], subtract(cube["b"], cube["a"]))
    cube_face = group(defs, "cube-left-face", "One central cube face, mirrored for the right side.")
    face(cube_face, "cube-frame", cube, "frame-top frame-front frame-bottom frame-left-bottom", "#0b3049")
    face(cube_face, "cube-glass", cube, "a b c d", "#d4b878")
    face(cube_face, "cube-floor", cube, "d back floor-right c", "#b9a77a")
    cube_edges = [
        *chain("panel-border", "a back-top b floor-right c d a", 1.4),
        Edge("panel-back", "back-top", "back", 4),
        *chain("panel-diagonal", "d back b", 4),
        Edge("panel-floor", "back", "floor-right", 4),
    ]
    return Network("cube-face-highlights", cube_face, cube, cube_edges, {}, True)


def build_document() -> tuple[etree._Element, list[Network]]:
    """Build the geometry before shading is sampled from the PNG reference."""
    root = etree.Element(
        SVG + "svg",
        nsmap={None: SVG[1:-1], "xlink": XLINK[1:-1]},
        width="1024",
        height="1024",
        viewBox="0 0 1024 1024",
        role="img",
        attrib={"aria-labelledby": "title description"},
    )
    etree.SubElement(root, SVG + "title", id="title").text = "MindRoom illuminated glass M"
    etree.SubElement(
        root,
        SVG + "desc",
        id="description",
    ).text = "Editable vector faces and joined glass highlights. Geometry is generated from named vertices in artwork.py; no raster image is embedded."
    defs = etree.SubElement(root, SVG + "defs")

    networks = room_networks(defs)
    networks.append(cube_face_network(defs))

    background = group(root, "background", "Delete this group for a transparent canvas.")
    etree.SubElement(background, SVG + "path", id="background-color", d="M 0,0 H 1024 V 1024 H 0 Z", fill="#22536c")
    logo = group(root, "logo", "Artwork layers: frame, room wings, then the illuminated central cube.")
    frame = group(logo, "outer-frame", "The structural M is one continuous polygon.")
    etree.SubElement(
        frame,
        SVG + "path",
        id="structural-frame",
        d="M 168.5,222.5 311.5,134.5 512,264.5 712.5,134.5 855.5,222.5 V 741.5 L 735.5,809.5 599.5,719.5 V 589.5 L 512,643.5 424.5,589.5 V 719.5 L 288.5,809.5 168.5,741.5 Z",
        fill="#082b43",
    )
    etree.SubElement(logo, SVG + "use", id="left-rooms", attrib={XLINK + "href": "#room-wing"})
    etree.SubElement(
        logo,
        SVG + "use",
        id="right-rooms",
        transform="translate(1024 0) scale(-1 1)",
        attrib={XLINK + "href": "#room-wing"},
    )
    center = group(logo, "central-cube", "The top rim and center vertical share their front and back vertices.")
    etree.SubElement(center, SVG + "use", id="cube-left", attrib={XLINK + "href": "#cube-left-face"})
    etree.SubElement(
        center,
        SVG + "use",
        id="cube-right",
        transform="translate(1024 0) scale(-1 1)",
        attrib={XLINK + "href": "#cube-left-face"},
    )
    top = {
        "left": (358.0, 361.0),
        "back": (512.0, 265.0),
        "right": (666.0, 361.0),
        "front": (512.0, 455.0),
        "bottom": (512.0, 643.0),
        "a": (404.0, 364.0),
        "b": (512.0, 298.0),
        "c": (620.0, 364.0),
        "d": (512.0, 432.0),
    }
    face(center, "cube-top-frame", top, "left back right front", "#4e827e")
    face(center, "cube-top-glass", top, "a b c d", "#ddc48a")
    top_edges = [
        *chain("cube-back-rim", "a b c", 4.8),
        *chain("cube-front-rim", "a d c", 2),
        Edge("cube-back-vertical", "b", "d", 4),
        *chain("frame-front-rim", "left front right", 1.6),
        Edge("frame-center", "front", "bottom", 4),
    ]
    center_boundary = [(512.0, 265.0), (666.0, 361.0), (666.0, 546.0), (512.0, 643.0), (358.0, 546.0), (358.0, 361.0)]
    networks.append(
        Network("cube-top-highlights", center, top, top_edges, {"bottom": (1.0, 0.0)}, silhouette=center_boundary),
    )
    return root, networks
