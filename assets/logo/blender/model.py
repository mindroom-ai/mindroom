# ruff: noqa: INP001 -- Standalone Blender script, outside the application package.

"""The MindRoom M as a 3D model, lifted from the SVG drawing.

The SVG logo is an orthographic drawing of a real object. The named 2D corners
from ../artwork.py are lifted back into 3D here, so from the logo's camera the
model's corners line up with the drawing's. crystal.py builds the model with its
own materials, then stages and renders it.
"""

import math

import bmesh
import bpy
from mathutils import Matrix, Vector

# Projection implied by the drawing. One cube edge along a horizontal axis is
# drawn 154 px across and 96 px down, so the camera looks down at asin(96/154).
A, B = 154.0, 96.0
PHI = math.asin(B / A)
PX_PER_UNIT = A * math.sqrt(2)
C = PX_PER_UNIT * math.cos(PHI)  # Screen height of one vertical unit.
V0 = 643.5 - 2 * B  # Screen v of the origin, below the cube's front-bottom corner.
H = (V0 - 265.0) / C  # Cube height, from its back-top corner at (512, 265).


def lift(u: float, v: float, *, x: float | None = None, y: float | None = None, z: float | None = None) -> Vector:
    """Return the 3D point drawn at SVG (u, v) on one known axis-aligned plane."""
    d = (u - 512.0) / A  # y - x
    if z is not None:
        s = (v - V0 + C * z) / B  # x + y
        return Vector(((s - d) / 2, (s + d) / 2, z))
    if x is None:
        x = y - d
    else:
        y = x + d
    return Vector((x, y, (V0 + B * (x + y) - v) / C))


# Left wing, lifted from artwork.py vertex names. The wing runs along -y into
# the cube's back face; the right wing swaps x and y.
ROOF_LEFT = lift(189.0, 233.0, z=H)  # "roof-left"
ROOF_APEX = lift(311.0, 157.0, z=H)  # "roof-apex"
TOWER_TOP = lift(288.0, 295.0, z=H)  # "tower-top"
WING_X = (ROOF_APEX.x, (ROOF_LEFT.x + TOWER_TOP.x) / 2)
TOWER_Y = (ROOF_LEFT.y, TOWER_TOP.y)
TOWER_BOTTOM = (lift(288.0, 638.0, x=WING_X[1]).z + lift(189.0, 575.0, x=WING_X[1]).z) / 2  # "tower-foot"
BRIDGE_DEPTH = (320.0 - 295.0) / C  # "upper-beam" height.
WELL_APEX = lift(403.0, 237.5, z=H)  # "well-apex"
WELL_SIDE = lift(317.0, 295.0, z=H)  # "well-face-left"
WELL_FRONT = lift(397.0, 343.0, z=H)  # "well-face-front"
WELL_X = (WELL_APEX.x, (WELL_SIDE.x + WELL_FRONT.x) / 2)
WELL_Y = (WELL_APEX.y + WELL_SIDE.y) / 2
FOOT_Z = (
    sum(lift(u, v, x=WING_X[1]).z for u, v in [(189.0, 729.0), (287.5, 785.0)]) / 2,  # "foot-*-bottom"
    sum(lift(u, v, x=WING_X[1]).z for u, v in [(189.0, 604.0), (287.5, 660.0)]) / 2,  # "foot-*-top"
)
CUBE_BEAM = lift(382.5, 403.0, x=1.0).y  # Cube pane corner "a" on the left face.
CUBE_CENTER = Vector((0.5, 0.5, H / 2))

GLASS_BEVEL = (0.04, 6)  # Width and segments of the rounded glass edges.
VIEW_TARGET = lift(512.0, 512.0, x=0.5)  # The point at the center of the logo's view.


# ---------------------------------------------------------------- geometry


def add_box(bm: bmesh.types.BMesh, lo: tuple, hi: tuple) -> None:
    """Add an axis-aligned box."""
    lo, hi = Vector(lo), Vector(hi)
    matrix = Matrix.Translation((lo + hi) / 2) @ Matrix.Diagonal((*(hi - lo), 1.0))
    bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)


def add_prism(
    bm: bmesh.types.BMesh,
    profile: list[tuple[float, float]],
    x0: float,
    x1: float,
) -> None:
    """Extrude a closed (y, z) profile along x."""
    near = [bm.verts.new((x0, y, z)) for y, z in profile]
    far = [bm.verts.new((x1, y, z)) for y, z in profile]
    faces = [bm.faces.new(near), bm.faces.new(far)]
    faces += [bm.faces.new((near[i], near[i - 1], far[i - 1], far[i])) for i in range(len(profile))]
    bmesh.ops.recalc_face_normals(bm, faces=faces)


def swap_xy(bm: bmesh.types.BMesh) -> None:
    """Mirror geometry across the plane x = y, keeping normals outward."""
    for vert in bm.verts:
        vert.co = Vector((vert.co.y, vert.co.x, vert.co.z))
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])


def mesh_object(
    name: str,
    bm: bmesh.types.BMesh,
    material: bpy.types.Material | None,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Turn a BMesh into an object with one material, or none."""
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    if material:
        mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def bevel(obj: bpy.types.Object, width: float, segments: int = 3) -> None:
    """Round sharp edges so they catch the light like polished glass."""
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    modifier = obj.modifiers.new("Bevel", "BEVEL")
    modifier.width = width
    modifier.segments = segments
    modifier.limit_method = "ANGLE"
    modifier.harden_normals = True


def cutter(name: str, bm: bmesh.types.BMesh, collection: bpy.types.Collection) -> bpy.types.Object:
    """Turn a BMesh into a hidden boolean cutter."""
    # Booleans merge the operands' material lists; a cutter without one cannot remap the cut object's slots.
    obj = mesh_object(name, bm, None, collection)
    obj.display_type = "WIRE"
    obj.hide_render = True
    return obj


def subtract(obj: bpy.types.Object, tool: bpy.types.Object) -> None:
    """Cut a cutter out of an object with the exact boolean solver."""
    modifier = obj.modifiers.new(f"Cut {tool.name}", "BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.material_mode = "INDEX"  # Cut faces take the object's first material.
    modifier.object = tool


def build_wing(
    name: str,
    glass: bpy.types.Material,
    collection: bpy.types.Collection,
    cutters: bpy.types.Collection,
    *,
    mirror: bool,
) -> list[bpy.types.Object]:
    """Build one solid tower, its bridge into the cube, and its lower block."""
    x0, x1 = WING_X
    y0, y1 = TOWER_Y
    bm = bmesh.new()
    # Tower and bridge share the roof plane in the drawing; the bridge ends at the cube.
    profile = [
        (y0, TOWER_BOTTOM),
        (y1, TOWER_BOTTOM),
        (y1, H - BRIDGE_DEPTH),
        (0.0, H - BRIDGE_DEPTH),
        (0.0, H),
        (y0, H),
    ]
    add_prism(bm, profile, x0, x1)
    if mirror:
        swap_xy(bm)
    upper = mesh_object(f"{name}-tower", bm, glass, collection)
    bm = bmesh.new()
    well = [(WELL_Y, H - 2), (0.5, H - 2), (0.5, H + 1), (WELL_Y, H + 1)]
    add_prism(bm, well, *WELL_X)
    if mirror:
        swap_xy(bm)
    subtract(upper, cutter(f"{name}-well", bm, cutters))
    bevel(upper, *GLASS_BEVEL)

    bm = bmesh.new()
    add_box(bm, (x0, y0, FOOT_Z[0]), (x1, y1, FOOT_Z[1]))
    if mirror:
        swap_xy(bm)
    foot = mesh_object(f"{name}-foot", bm, glass, collection)
    bevel(foot, *GLASS_BEVEL)
    return [upper, foot]


def build_cube(
    paint: bpy.types.Material,
    glow: bpy.types.Material,
    collection: bpy.types.Collection,
    cutters: bpy.types.Collection,
) -> bpy.types.Object:
    """Build the open cube frame and the glowing bead at its center; return the frame."""
    w = CUBE_BEAM
    bm = bmesh.new()
    add_box(bm, (0.0, 0.0, 0.0), (1.0, 1.0, H))
    frame = mesh_object("cube-frame", bm, paint, collection)
    hi = (1.0, 1.0, H)
    for axis, label in enumerate("xyz"):
        lo_cut = [w, w, w]
        hi_cut = [1.0 - w, 1.0 - w, H - w]
        lo_cut[axis], hi_cut[axis] = -1.0, hi[axis] + 1.0
        bm = bmesh.new()
        add_box(bm, lo_cut, hi_cut)
        subtract(frame, cutter(f"cube-opening-{label}", bm, cutters))
    bevel(frame, 0.02, 4)

    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.08, matrix=Matrix.Translation(CUBE_CENTER))
    core = mesh_object("cube-core", bm, glow, collection)
    for polygon in core.data.polygons:
        polygon.use_smooth = True
    core.visible_shadow = False  # A core light sits inside this glowing bead.
    return frame


# ---------------------------------------------------------------- scene


def add_camera(name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """Create a camera object."""
    camera = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    collection.objects.link(camera)
    return camera


def logo_camera(name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """The SVG's orthographic camera: the one view in which the parts form the M."""
    camera = add_camera(name, collection)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 1024.0 / PX_PER_UNIT
    camera.rotation_euler = (math.pi / 2 - PHI, 0.0, math.radians(135.0))
    toward = Vector((math.cos(PHI) / math.sqrt(2), math.cos(PHI) / math.sqrt(2), math.sin(PHI)))
    camera.location = VIEW_TARGET + 30.0 * toward
    return camera


def new_collection(name: str, parent: bpy.types.Collection | None = None) -> bpy.types.Collection:
    """Create and link a collection."""
    collection = bpy.data.collections.new(name)
    (parent or bpy.context.scene.collection).children.link(collection)
    return collection


def build_model(glass: bpy.types.Material, paint: bpy.types.Material, glow: bpy.types.Material) -> list:
    """Build the M: glass wings, the painted cube frame, and its glowing bead.

    Returns the cube frame followed by the left tower and foot and the right tower and foot.
    """
    model = new_collection("mindroom-logo")
    cutters = new_collection("cutters", model)
    frame = build_cube(paint, glow, model, cutters)
    wings = [
        obj
        for name, mirror in [("left", False), ("right", True)]
        for obj in build_wing(name, glass, model, cutters, mirror=mirror)
    ]
    return [frame, *wings]
