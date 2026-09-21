# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.

"""Fit the reference illumination to editable SVG gradients and vector masks.

Geometry owns every pixel before colors are sampled. Surface colors extend
from that surface's interior, so the PNG cannot introduce new edge geometry.
"""

from dataclasses import dataclass
from io import BytesIO

import numpy as np
import resvg_py
from artwork import SVG
from geometry import Edge, Point
from lxml import etree
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage

type Colors = NDArray[np.float64]


@dataclass(frozen=True)
class SurfaceGrid:
    """A bounded rectangular color grid sampled from one visible surface."""

    x0: int
    y0: int
    x1: int
    y1: int
    colors: Colors


def render(root: etree._Element, size: int = 1024, *, height: int | None = None) -> bytes:
    """Rasterize the entire canvas; preserve the viewBox and paint coordinates."""
    return resvg_py.svg_to_bytes(
        svg_string=etree.tostring(root, encoding="unicode"),
        width=size,
        height=size if height is None else height,
    )


def pixels(png: bytes) -> NDArray[np.uint8]:
    """Decode RGBA pixels so output verification also checks transparency."""
    with Image.open(BytesIO(png)) as image:
        return np.asarray(image.convert("RGBA"))


def color_hex(color: Colors) -> str:
    """Round a bounded sampled color to an SVG hexadecimal color."""
    red, green, blue = np.clip(np.rint(color), 0, 255).astype(int)
    return f"#{red:02x}{green:02x}{blue:02x}"


def sample(target: Colors, xs: Colors, ys: Colors) -> Colors:
    """Sample RGB at floating-point positions using bilinear interpolation."""
    coordinates = [np.atleast_1d(ys), np.atleast_1d(xs)]
    colors = np.stack(
        [ndimage.map_coordinates(target[:, :, channel], coordinates, order=1, mode="nearest") for channel in range(3)],
        axis=-1,
    )
    return colors.reshape((*xs.shape, 3))


def fit_surface(mask: NDArray[np.uint8], target: Colors, step: int) -> SurfaceGrid | None:
    """Continue clean interior colors to a face's boundary, then sample a grid."""
    active = mask > 240
    eroded = ndimage.binary_erosion(active, iterations=3)
    if eroded.sum() > 30:
        active = eroded
    if active.sum() < 3:
        active = mask > 40
    if active.sum() < 3:
        return None
    ys, xs = np.nonzero(mask > 10)
    x0, x1 = max(0, int(xs.min()) - 4), min(1024, int(xs.max()) + 5)
    y0, y1 = max(0, int(ys.min()) - 4), min(1024, int(ys.max()) + 5)
    x_step = min(step, 2) if x1 - x0 < 35 else step
    nx = max(2, int(np.ceil((x1 - x0) / x_step)) + 1)
    ny = max(2, int(np.ceil((y1 - y0) / step)) + 1)

    nearest = ndimage.distance_transform_edt(~active, return_distances=False, return_indices=True)
    smooth = ndimage.gaussian_filter(target[nearest[0], nearest[1]], sigma=(1.0, 0.7, 0))
    if x1 - x0 >= 35:
        # Normalized convolution prevents nearest-neighbor stair steps along
        # diagonal boundaries from becoming scallops in the sampled lighting.
        weight = ndimage.gaussian_filter(active.astype(float), sigma=3)
        color_sum = ndimage.gaussian_filter(target * active[:, :, None], sigma=(3, 3, 0))
        supported = weight > 1e-6
        smooth[supported] = color_sum[supported] / weight[supported, None]
    yy, xx = np.meshgrid(np.linspace(y0, y1, ny), np.linspace(x0, x1, nx), indexing="ij")
    return SurfaceGrid(x0, y0, x1, y1, sample(smooth, xx, yy))


def add_band(defs: etree._Element, pattern: etree._Element, name: str, grid: SurfaceGrid, row: int) -> None:
    """Blend adjacent horizontal gradients in one clipped vertical band."""
    height = (grid.y1 - grid.y0) / (grid.colors.shape[0] - 1)
    y = grid.y0 + row * height
    fade_id, mask_id, clip_id = (f"{name}-{kind}-{row}" for kind in ("fade", "mask", "band"))
    fade = etree.SubElement(
        defs,
        SVG + "linearGradient",
        id=fade_id,
        x1="0",
        y1=f"{y:.6f}",
        x2="0",
        y2=f"{y + height:.6f}",
        gradientUnits="userSpaceOnUse",
    )
    etree.SubElement(fade, SVG + "stop", offset="0", attrib={"stop-color": "#ffffff"})
    etree.SubElement(fade, SVG + "stop", offset="1", attrib={"stop-color": "#000000"})
    bounds = {
        "x": str(grid.x0 - 4),
        "y": f"{y - 8:.6f}",
        "width": str(grid.x1 - grid.x0 + 8),
        "height": f"{height + 16:.6f}",
    }
    mask = etree.SubElement(
        defs,
        SVG + "mask",
        id=mask_id,
        maskUnits="userSpaceOnUse",
        maskContentUnits="userSpaceOnUse",
        **bounds,
    )
    etree.SubElement(mask, SVG + "rect", **bounds, fill=f"url(#{fade_id})")
    clip = etree.SubElement(defs, SVG + "clipPath", id=clip_id)
    etree.SubElement(
        clip,
        SVG + "rect",
        x=str(grid.x0),
        y=f"{y:.6f}",
        width=str(grid.x1 - grid.x0),
        height=f"{height + 1:.6f}",
    )
    band = etree.SubElement(pattern, SVG + "g", attrib={"clip-path": f"url(#{clip_id})"})
    rectangle = {"x": str(grid.x0), "y": f"{y - 2:.6f}", "width": str(grid.x1 - grid.x0), "height": f"{height + 4:.6f}"}
    etree.SubElement(band, SVG + "rect", **rectangle, fill=f"url(#{name}-row-{row + 1})")
    etree.SubElement(band, SVG + "rect", **rectangle, fill=f"url(#{name}-row-{row})", mask=f"url(#{mask_id})")


def surface_paint(defs: etree._Element, name: str, grid: SurfaceGrid) -> str:
    """Encode bilinear surface shading using standard SVG 1.1 paint servers."""
    ny, nx, _ = grid.colors.shape
    for row, colors in enumerate(grid.colors):
        gradient = etree.SubElement(
            defs,
            SVG + "linearGradient",
            id=f"{name}-row-{row}",
            x1="0%",
            y1="0%",
            x2="100%",
            y2="0%",
        )
        for col, color in enumerate(colors):
            etree.SubElement(
                gradient,
                SVG + "stop",
                offset=f"{col / (nx - 1):.6f}",
                attrib={"stop-color": color_hex(color)},
            )
    pattern = etree.SubElement(
        defs,
        SVG + "pattern",
        id=name,
        patternUnits="userSpaceOnUse",
        width="1024",
        height="1024",
        attrib={"shape-rendering": "crispEdges"},
    )
    etree.SubElement(pattern, SVG + "rect", width="1024", height="1024", fill=color_hex(grid.colors.mean(axis=(0, 1))))
    for row in range(ny - 1):
        add_band(defs, pattern, name, grid, row)
    return f"url(#{name})"


def shade_surfaces(root: etree._Element, defs: etree._Element, faces: list[etree._Element], target: Colors) -> None:
    """Measure surface ownership before replacing black mask paint with color."""
    symmetric = (target + target[:, ::-1]) / 2
    fits = []
    for face in faces:
        face.set("fill", "#000000")
    for face in faces:
        face.set("fill", "#ffffff")
        mask = pixels(render(root))[:, :, 0].copy()
        face.set("fill", "#000000")
        mirrored = any(parent.get("id") in {"room-wing", "cube-left-face"} for parent in face.iterancestors())
        if mirrored:
            mask[:, 512:] = 0
        name = face.get("id")
        fits.append(fit_surface(mask, symmetric if mirrored else target, 24 if name == "background-color" else 8))
    defs.append(etree.Comment(" Generated surface lighting: horizontal color rows blended by vertical vector masks. "))
    for face, grid in zip(faces, fits):
        if grid is None:
            msg = f"Surface has no visible pixels: {face.get('id')}"
            raise ValueError(msg)
        face.set("fill", surface_paint(defs, f"paint-{face.get('id')}", grid))


def edge_paint(defs: etree._Element, name: str, points: dict[str, Point], edge: Edge, target: Colors) -> str:
    """Sample lighting along an edge while sharing exact endpoint colors."""
    start, end = np.array(points[edge.start]), np.array(points[edge.end])
    length = np.linalg.norm(end - start)
    count = max(4, int(np.ceil(length)) + 1)
    ts = np.linspace(0, 1, count)
    positions = start + ts[:, None] * (end - start)
    normal = np.array([-(end - start)[1], (end - start)[0]]) / length
    samples = [sample(target, *(positions + normal * offset).T) for offset in (-0.4, 0, 0.4)]
    colors = ndimage.gaussian_filter1d(np.mean(samples, axis=0), sigma=2, axis=0)
    # Every incident edge ends in the same color. Separate one-dimensional
    # blurs otherwise introduce a visible color mismatch at a closed joint.
    start_color = sample(target, np.array(start[0]), np.array(start[1]))
    end_color = sample(target, np.array(end[0]), np.array(end[1]))
    for index, t in enumerate(ts):
        start_weight, end_weight = max(0, 1 - t * length / 3), max(0, 1 - (1 - t) * length / 3)
        colors[index] = (colors[index] + start_weight * start_color + end_weight * end_color) / (
            1 + start_weight + end_weight
        )
    colors[0], colors[-1] = start_color, end_color
    gradient = etree.SubElement(
        defs,
        SVG + "linearGradient",
        id=name,
        gradientUnits="userSpaceOnUse",
        x1=str(start[0]),
        y1=str(start[1]),
        x2=str(end[0]),
        y2=str(end[1]),
    )
    for index in sorted(set(range(0, count, 4)) | {count - 1}):
        attributes = {"stop-color": color_hex(colors[index])}
        if edge.fade is not None:
            opacity = min(1, max(0, (1 - ts[index]) / (1 - edge.fade)))
            attributes["stop-opacity"] = f"{opacity * opacity:.6f}"
        etree.SubElement(gradient, SVG + "stop", offset=f"{ts[index]:.6f}", attrib=attributes)
    return f"url(#{name})"
