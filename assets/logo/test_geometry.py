# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.

"""Regression checks for the logo's shared, angled highlight junctions."""

from math import cos, pi, sin

import pytest
from geometry import Edge, joined_polygons


def contains(polygon: list[tuple[float, float]], point: tuple[float, float]) -> bool:
    """Use an independent ray test to check coverage of a polygon interior."""
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


@pytest.mark.parametrize("count", [2, 3, 4])
def test_join_has_no_hole_or_overlapping_faces(count: int) -> None:
    """A corner's pieces partition its center rather than leaving flat-cap gaps."""
    angles = [0, pi / 2] if count == 2 else [i * 2 * pi / count for i in range(count)]
    points = {"center": (0.0, 0.0)} | {str(i): (20 * cos(a), 20 * sin(a)) for i, a in enumerate(angles)}
    edges = [Edge(f"edge-{i}", "center", str(i), 4.0) for i in range(count)]
    polygons = joined_polygons(points, edges)
    for i in range(41):
        angle = (i + 0.173) * 2 * pi / 41
        point = (1.4 * cos(angle), 1.4 * sin(angle))
        assert sum(contains(p, point) for p in polygons.values()) == 1


def test_two_edges_meet_at_a_miter_instead_of_butt_caps() -> None:
    """A right-angle join covers the outer miter and preserves the inner cut."""
    points = {"joint": (0.0, 0.0), "east": (20.0, 0.0), "south": (0.0, 20.0)}
    polygons = joined_polygons(points, [Edge("a", "joint", "east", 4), Edge("b", "joint", "south", 4)])
    assert any(contains(p, (-1.5, -1.4)) for p in polygons.values())
    assert not any(contains(p, (2.2, 2.3)) for p in polygons.values())


def test_unequal_width_three_way_join_is_closed() -> None:
    """A narrow glass border can meet a wide rim and vertical without a hole."""
    points = {"joint": (0.0, 0.0), "a": (-20.0, -12.0), "b": (20.0, -12.0), "c": (0.0, 20.0)}
    edges = [Edge("a", "joint", "a", 4), Edge("b", "joint", "b", 4), Edge("c", "joint", "c", 5)]
    polygons = joined_polygons(points, edges)
    for i in range(31):
        angle = (i + 0.219) * 2 * pi / 31
        point = (1.3 * cos(angle), 1.3 * sin(angle))
        assert sum(contains(p, point) for p in polygons.values()) == 1


def test_straight_continuation_has_no_seam() -> None:
    """Opposite collinear edges share the same cross-section."""
    points = {"joint": (0.0, 0.0), "a": (-20.0, 0.0), "b": (20.0, 0.0)}
    polygons = joined_polygons(points, [Edge("a", "a", "joint", 4), Edge("b", "joint", "b", 4)])
    for point in [(-0.01, 1.9), (0.01, 1.9), (-0.01, -1.9), (0.01, -1.9)]:
        assert any(contains(p, point) for p in polygons.values())


def test_terminal_cut_follows_the_occluding_plane() -> None:
    """A vertical strut's end is cut diagonally against the beam above it."""
    points = {"top": (0.0, 0.0), "bottom": (0.0, 20.0)}
    polygon = joined_polygons(points, [Edge("strut", "top", "bottom", 4)], cuts={"top": (1.0, 0.5)})["strut"]
    assert contains(polygon, (-1.5, 0.0))
    assert not contains(polygon, (1.5, 0.0))


def test_edge_too_short_for_its_miters_is_rejected() -> None:
    """Moving neighboring corners too close reports an error instead of a bow tie."""
    points = {"a": (0.0, 0.0), "b": (0.5, 0.0), "up-a": (0.0, -20.0), "up-b": (0.5, -20.0)}
    edges = [Edge("short", "a", "b", 4), Edge("left", "a", "up-a", 4), Edge("right", "b", "up-b", 4)]
    with pytest.raises(ValueError, match="too short"):
        joined_polygons(points, edges)
