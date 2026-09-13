# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.

"""Shared-vertex geometry for the MindRoom logo's glass-edge highlights."""

from collections import defaultdict
from dataclasses import dataclass
from math import atan2, hypot, isfinite

type Point = tuple[float, float]


@dataclass(frozen=True)
class Edge:
    """A highlighted segment joining two named vertices."""

    name: str
    start: str
    end: str
    width: float
    fade: float | None = None


def add(a: Point, b: Point) -> Point:
    """Add two vectors."""
    return a[0] + b[0], a[1] + b[1]


def subtract(a: Point, b: Point) -> Point:
    """Return the vector from b to a."""
    return a[0] - b[0], a[1] - b[1]


def scale(a: Point, factor: float) -> Point:
    """Scale a vector."""
    return a[0] * factor, a[1] * factor


def cross(a: Point, b: Point) -> float:
    """Return the signed two-dimensional cross product."""
    return a[0] * b[1] - a[1] * b[0]


def intersection(a: Point, direction_a: Point, b: Point, direction_b: Point) -> Point:
    """Intersect two infinite lines, each given by a point and direction."""
    determinant = cross(direction_a, direction_b)
    if abs(determinant) < 1e-10:
        msg = "Cannot intersect parallel lines"
        raise ValueError(msg)
    return add(a, scale(direction_a, cross(subtract(b, a), direction_b) / determinant))


def _incident_edges(points: dict[str, Point], edges: list[Edge]) -> dict[str, list[tuple[str, Point, float]]]:
    """Validate edges and collect their outward directions at each vertex."""
    incident: dict[str, list[tuple[str, Point, float]]] = defaultdict(list)
    names: set[str] = set()
    for edge in edges:
        if edge.name in names or edge.width <= 0 or not isfinite(edge.width):
            msg = f"Duplicate edge name or invalid width: {edge.name}"
            raise ValueError(msg)
        names.add(edge.name)
        a, b = points[edge.start], points[edge.end]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = hypot(dx, dy)
        if length < 1e-8:
            msg = f"Zero-length edge: {edge.name}"
            raise ValueError(msg)
        direction = (dx / length, dy / length)
        incident[edge.start].append((edge.name, direction, edge.width / 2))
        incident[edge.end].append((edge.name, scale(direction, -1), edge.width / 2))

    return incident


def joined_polygons(
    points: dict[str, Point],
    edges: list[Edge],
    *,
    cuts: dict[str, Point] | None = None,
) -> dict[str, list[Point]]:
    """Build the filled highlight polygon belonging to each edge."""
    cuts = cuts or {}
    incident = _incident_edges(points, edges)

    # Each pair of neighboring edges owns one shared miter point. All edge
    # faces also reach the named center vertex, closing three- and four-way
    # corners without overlapping independently capped strokes.
    caps: dict[tuple[str, str], tuple[Point, Point]] = {}
    for node, outgoing in incident.items():
        center = points[node]
        outgoing.sort(key=lambda item: atan2(item[1][1], item[1][0]))
        if len(outgoing) == 1:
            name, direction, half = outgoing[0]
            normal = (-direction[1], direction[0])
            left, right = add(center, scale(normal, half)), add(center, scale(normal, -half))
            if node in cuts:
                left = intersection(left, direction, center, cuts[node])
                right = intersection(right, direction, center, cuts[node])
            caps[node, name] = left, right
            continue
        miters: list[Point] = []
        for first, second in zip(outgoing, outgoing[1:] + outgoing[:1]):
            _, a, half_a = first
            _, b, half_b = second
            left = add(center, (-a[1] * half_a, a[0] * half_a))
            right = add(center, (b[1] * half_b, -b[0] * half_b))
            if abs(cross(a, b)) < 1e-8:
                if a[0] * b[0] + a[1] * b[1] > 0:
                    msg = f"Overlapping edges leave vertex {node} in the same direction"
                    raise ValueError(msg)
                miter = scale(add(left, right), 0.5)
            else:
                miter = intersection(left, a, right, b)
            if hypot(*subtract(miter, center)) > 8 * max(half_a, half_b):
                msg = f"Excessively acute junction at {node}; split the geometry explicitly"
                raise ValueError(msg)
            miters.append(miter)
        for index, (name, _, _) in enumerate(outgoing):
            caps[node, name] = miters[index], miters[index - 1]

    return {edge.name: _edge_polygon(edge, points, caps) for edge in edges}


def _edge_polygon(
    edge: Edge,
    points: dict[str, Point],
    caps: dict[tuple[str, str], tuple[Point, Point]],
) -> list[Point]:
    """Build one facet, rejecting crossed miters when an edge is too short."""
    left_a, right_a = caps[edge.start, edge.name]
    left_b, right_b = caps[edge.end, edge.name]
    direction = subtract(points[edge.end], points[edge.start])
    for start, end in ((left_a, right_b), (right_a, left_b)):
        dx, dy = subtract(end, start)
        if dx * direction[0] + dy * direction[1] < -1e-8:
            msg = f"Edge {edge.name} is too short for its joined widths; separate the corners or reduce the widths"
            raise ValueError(msg)
    return [points[edge.start], left_a, right_b, points[edge.end], left_b, right_a]
