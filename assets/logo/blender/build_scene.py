# ruff: noqa: INP001 -- Standalone Blender script, outside the application package.

"""Build the 3D MindRoom logo in Blender, then save and render it.

Run from the repository root:

    blender --background --factory-startup --python assets/logo/blender/build_scene.py -- --render

The SVG logo is an orthographic drawing of a real object.
The named 2D corners from ../artwork.py are lifted back into 3D here,
so the hero camera reproduces the SVG silhouette while every other angle
shows consistent geometry.
"""

import argparse
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent

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
TOWER_WALL = (308.5 - 288.0) / A  # "tower-gold" is the inner face of the front wall.
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
# "floor-back" is the cavity corner seen through the inner glass face.
FOOT_WALL = (512.0 + A * (TOWER_Y[1] - WING_X[0]) - 387.0) / (2 * A)
CUBE_BEAM = lift(382.5, 403.0, x=1.0).y  # Cube pane corner "a" on the left face.
CUBE_CENTER = Vector((0.5, 0.5, H / 2))

HOLLOW = True  # The SVG draws hollow glass rooms; a solid-crystal look can turn this off.
GLASS_BEVEL = (0.012, 3)  # Width and segments of the rounded glass edges.

# Screen-space hero framing: the SVG center and the outer navy frame.
HERO_TARGET = lift(512.0, 512.0, x=0.5)
STRUCTURAL_FRAME = [
    (168.5, 222.5), (311.5, 134.5), (512.0, 264.5), (712.5, 134.5), (855.5, 222.5), (855.5, 741.5),
    (735.5, 809.5), (599.5, 719.5), (599.5, 589.5), (512.0, 643.5), (424.5, 589.5), (424.5, 719.5),
    (288.5, 809.5), (168.5, 741.5),
]  # fmt: skip


def srgb(hex_color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    """Convert an sRGB hex color to linear RGBA."""
    channels = [int(hex_color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return (*linear, alpha)


# ---------------------------------------------------------------- geometry


def add_box(bm: bmesh.types.BMesh, lo: tuple, hi: tuple, *, inward: bool = False) -> None:
    """Add an axis-aligned box; inward boxes bound a cavity inside glass and are tagged `cavity`."""
    lo, hi = Vector(lo), Vector(hi)
    # One material must cover both shells, or Cycles never leaves the glass volume at the cavity.
    # Adding a layer invalidates face references, so it exists before the box does.
    tag = bm.faces.layers.float.get("cavity") or bm.faces.layers.float.new("cavity")
    matrix = Matrix.Translation((lo + hi) / 2) @ Matrix.Diagonal((*(hi - lo), 1.0))
    verts = bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)["verts"]
    if inward:
        faces = list({f for v in verts for f in v.link_faces})
        bmesh.ops.reverse_faces(bm, faces=faces)
        for face in faces:
            face[tag] = 1.0


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
    material: bpy.types.Material,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Turn a BMesh into an object with one material."""
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
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
    obj = mesh_object(name, bm, bpy.data.materials["navy-frame"], collection)
    # Booleans merge the operands' material lists; a cutter without one cannot remap the cut object's slots.
    obj.data.materials.clear()
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
    materials: dict[str, bpy.types.Material],
    collection: bpy.types.Collection,
    cutters: bpy.types.Collection,
    *,
    mirror: bool,
) -> list[bpy.types.Object]:
    """Build one tower, its bridge into the cube, and its lower glass block."""
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
    t = TOWER_WALL
    if HOLLOW:
        add_box(bm, (x0 + t, y0 + t, TOWER_BOTTOM + t), (x1 - t, y1 - t, H - t), inward=True)
    if mirror:
        swap_xy(bm)
    upper = mesh_object(f"{name}-tower", bm, materials["glass"], collection)
    bm = bmesh.new()
    well = [(WELL_Y, H - 2), (0.5, H - 2), (0.5, H + 1), (WELL_Y, H + 1)]
    add_prism(bm, well, *WELL_X)
    if mirror:
        swap_xy(bm)
    subtract(upper, cutter(f"{name}-well", bm, cutters))
    bevel(upper, *GLASS_BEVEL)

    bm = bmesh.new()
    t = FOOT_WALL
    add_box(bm, (x0, y0, FOOT_Z[0]), (x1, y1, FOOT_Z[1]))
    if HOLLOW:
        add_box(bm, (x0 + t, y0 + t, FOOT_Z[0] + t), (x1 - t, y1 - t, FOOT_Z[1] - t), inward=True)
    if mirror:
        swap_xy(bm)
    foot = mesh_object(f"{name}-foot", bm, materials["glass"], collection)
    bevel(foot, *GLASS_BEVEL)
    return [upper, foot]


def build_cube(
    materials: dict[str, bpy.types.Material],
    collection: bpy.types.Collection,
    cutters: bpy.types.Collection,
) -> list[bpy.types.Object]:
    """Build the navy cube frame, its six warm glass panes, and the light core."""
    w = CUBE_BEAM
    bm = bmesh.new()
    add_box(bm, (0.0, 0.0, 0.0), (1.0, 1.0, H))
    frame = mesh_object("cube-frame", bm, materials["frame"], collection)
    hi = (1.0, 1.0, H)
    for axis, label in enumerate("xyz"):
        lo_cut = [w, w, w]
        hi_cut = [1.0 - w, 1.0 - w, H - w]
        lo_cut[axis], hi_cut[axis] = -1.0, hi[axis] + 1.0
        bm = bmesh.new()
        add_box(bm, lo_cut, hi_cut)
        subtract(frame, cutter(f"cube-opening-{label}", bm, cutters))
    bevel(frame, 0.01)

    inset, thickness, overlap = 0.03, 0.014, 0.02
    bm = bmesh.new()
    for axis in range(3):
        for side in (0.0, hi[axis]):
            lo_pane = [w - overlap, w - overlap, w - overlap]
            hi_pane = [1.0 - w + overlap, 1.0 - w + overlap, H - w + overlap]
            outer = side + (inset if side == 0.0 else -inset)
            lo_pane[axis], hi_pane[axis] = sorted((outer, outer + (thickness if side == 0.0 else -thickness)))
            add_box(bm, lo_pane, hi_pane)
    panes = mesh_object("cube-panes", bm, materials["pane"], collection)

    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.08, matrix=Matrix.Translation(CUBE_CENTER))
    core = mesh_object("cube-core", bm, materials["core"], collection)
    for polygon in core.data.polygons:
        polygon.use_smooth = True
    core.visible_shadow = False  # The core light sits inside this glowing bead.
    return [frame, panes, core]


# ---------------------------------------------------------------- materials


def node_material(name: str) -> tuple[bpy.types.Material, bpy.types.NodeTree]:
    """Create an empty node material."""
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    tree.nodes.clear()
    return material, tree


def add_opal_layer(
    tree: bpy.types.NodeTree,
    surface: bpy.types.NodeSocket,
    frost: float,
    color: tuple,
) -> bpy.types.NodeSocket:
    """Mix a faint diffuse layer into glass, so faces glow in the color of the light reaching them.

    It shows gold beside the core and teal under the sky, as painted in the SVG. Only the outer
    skin carries it: seen from inside the glass, or on cavity walls facing the hollow, lit
    surfaces would tint the glass green.
    """
    nodes, links = tree.nodes, tree.links
    opal = nodes.new("ShaderNodeBsdfDiffuse")
    opal.inputs["Color"].default_value = color
    geometry = nodes.new("ShaderNodeNewGeometry")
    cavity = nodes.new("ShaderNodeAttribute")
    cavity.attribute_type = "GEOMETRY"
    cavity.attribute_name = "cavity"
    inner = nodes.new("ShaderNodeMath")
    inner.operation = "MAXIMUM"
    links.new(geometry.outputs["Backfacing"], inner.inputs[0])
    links.new(cavity.outputs["Fac"], inner.inputs[1])
    outside = nodes.new("ShaderNodeMath")
    outside.operation = "MULTIPLY_ADD"
    links.new(inner.outputs["Value"], outside.inputs[0])
    outside.inputs[1].default_value = -frost
    outside.inputs[2].default_value = frost
    frosted = nodes.new("ShaderNodeMixShader")
    links.new(outside.outputs["Value"], frosted.inputs["Fac"])
    links.new(surface, frosted.inputs[1])
    links.new(opal.outputs["BSDF"], frosted.inputs[2])
    return frosted.outputs["Shader"]


def glass_material(
    name: str,
    color: tuple,
    roughness: float,
    shadow: tuple,
    absorption: tuple | None = None,
    absorption_density: float = 0.0,
    frost: float = 0.0,
    opal_color: tuple = (1.0, 1.0, 1.0, 1.0),
    glow: float = 0.0,
) -> bpy.types.Material:
    """Glass that tints light passing through it instead of blocking direct light."""
    material, tree = node_material(name)
    nodes, links = tree.nodes, tree.links
    output = nodes.new("ShaderNodeOutputMaterial")
    glass = nodes.new("ShaderNodeBsdfPrincipled")
    glass.inputs["Base Color"].default_value = color
    glass.inputs["Roughness"].default_value = roughness
    glass.inputs["IOR"].default_value = 1.45
    glass.inputs["Transmission Weight"].default_value = 1.0
    surface = glass.outputs["BSDF"]
    if frost:
        surface = add_opal_layer(tree, surface, frost, opal_color)
    if glow:
        # A translucent layer glows evenly with light from behind, hiding what lies beyond.
        translucent = nodes.new("ShaderNodeBsdfTranslucent")
        translucent.inputs["Color"].default_value = color
        glowing = nodes.new("ShaderNodeMixShader")
        glowing.inputs["Fac"].default_value = glow
        links.new(surface, glowing.inputs[1])
        links.new(translucent.outputs["BSDF"], glowing.inputs[2])
        surface = glowing.outputs["Shader"]
    # Shadow rays pass through tinted, so the core lights the wings without caustic noise.
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.inputs["Color"].default_value = shadow
    light_path = nodes.new("ShaderNodeLightPath")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(light_path.outputs["Is Shadow Ray"], mix.inputs["Fac"])
    links.new(surface, mix.inputs[1])
    links.new(transparent.outputs["BSDF"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])
    if absorption_density:
        # Thick glass deepens toward navy.
        absorb = nodes.new("ShaderNodeVolumeAbsorption")
        absorb.inputs["Color"].default_value = absorption
        absorb.inputs["Density"].default_value = absorption_density
        links.new(absorb.outputs["Volume"], output.inputs["Volume"])
    return material


def frame_material() -> bpy.types.Material:
    """Lacquered navy for the structural cube frame."""
    material, tree = node_material("navy-frame")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    paint = tree.nodes.new("ShaderNodeBsdfPrincipled")
    paint.inputs["Base Color"].default_value = srgb("#0b3049")
    paint.inputs["Roughness"].default_value = 0.55
    paint.inputs["Specular IOR Level"].default_value = 0.3
    paint.inputs["Coat Weight"].default_value = 0.15
    paint.inputs["Coat Roughness"].default_value = 0.06
    tree.links.new(paint.outputs["BSDF"], output.inputs["Surface"])
    return material


def emission_material(name: str, color: tuple, strength: float) -> bpy.types.Material:
    """Unlit emitter."""
    material, tree = node_material(name)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = strength
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def exact_emission(tree: bpy.types.NodeTree, color: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Emit a color that displays as the same sRGB value after the PBR Neutral view.

    PBR Neutral subtracts 0.04 - (min(sqrt(m), 0.2) - 0.2)^2 from every channel,
    where m is the darkest channel. Adding that offset back keeps unlit staging
    colors identical to the SVG.
    """
    nodes, links = tree.nodes, tree.links

    def math(
        operation: str,
        a: bpy.types.NodeSocket | float,
        b: bpy.types.NodeSocket | float | None = None,
    ) -> bpy.types.NodeSocket:
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for socket, value in zip(node.inputs, (a, b), strict=False):
            if isinstance(value, bpy.types.NodeSocket):
                links.new(value, socket)
            elif value is not None:
                socket.default_value = value
        return node.outputs["Value"]

    channels = nodes.new("ShaderNodeSeparateColor")
    links.new(color, channels.inputs["Color"])
    darkest = math("MINIMUM", math("MINIMUM", channels.outputs[0], channels.outputs[1]), channels.outputs[2])
    toe = math("POWER", math("SUBTRACT", math("MINIMUM", math("SQRT", darkest), 0.2), 0.2), 2.0)
    offset = math("SUBTRACT", 0.04, toe)
    restored = nodes.new("ShaderNodeVectorMath")
    restored.operation = "ADD"
    links.new(color, restored.inputs[0])
    links.new(offset, restored.inputs[1])
    emission = nodes.new("ShaderNodeEmission")
    links.new(restored.outputs["Vector"], emission.inputs["Color"])
    return emission.outputs["Emission"]


def staging_material(name: str, color: tuple) -> bpy.types.Material:
    """Flat SVG color for the hero set."""
    material, tree = node_material(name)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    rgb = tree.nodes.new("ShaderNodeRGB")
    rgb.outputs["Color"].default_value = color
    tree.links.new(exact_emission(tree, rgb.outputs["Color"]), output.inputs["Surface"])
    return material


def backdrop_material() -> bpy.types.Material:
    """The SVG background: a vertical teal-to-navy ramp with a soft halo behind the M."""
    material, tree = node_material("backdrop")
    nodes, links = tree.nodes, tree.links
    output = nodes.new("ShaderNodeOutputMaterial")
    coords = nodes.new("ShaderNodeTexCoord")
    separate = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["UV"], separate.inputs["Vector"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = srgb("#0f3755")
    ramp.color_ramp.elements[1].color = srgb("#3a7989")
    links.new(separate.outputs["Y"], ramp.inputs["Fac"])
    distance = nodes.new("ShaderNodeVectorMath")
    distance.operation = "DISTANCE"
    distance.inputs[1].default_value = (0.5, 0.68, 0.0)
    links.new(coords.outputs["UV"], distance.inputs[0])
    falloff = nodes.new("ShaderNodeMapRange")
    falloff.inputs["From Min"].default_value = 0.0
    falloff.inputs["From Max"].default_value = 0.42
    falloff.inputs["To Min"].default_value = 1.0
    falloff.inputs["To Max"].default_value = 0.0
    falloff.interpolation_type = "SMOOTHERSTEP"
    links.new(distance.outputs["Value"], falloff.inputs["Value"])
    halo = nodes.new("ShaderNodeMix")
    halo.data_type = "RGBA"
    halo.blend_type = "SCREEN"
    halo.inputs["B"].default_value = srgb("#4f8a8e")
    links.new(falloff.outputs["Result"], halo.inputs["Factor"])
    links.new(ramp.outputs["Color"], halo.inputs["A"])
    links.new(exact_emission(tree, halo.outputs["Result"]), output.inputs["Surface"])
    return material


def build_materials() -> dict[str, bpy.types.Material]:
    """Create every material used by the logo and its hero set."""
    teal = {
        "color": srgb("#a5d6e0"),
        "roughness": 0.06,
        "shadow": srgb("#9fdde3"),
        "absorption": srgb("#5aa5c8"),
        "absorption_density": 1.4,
    }
    return {
        "frame": frame_material(),
        "glass": glass_material("teal-glass", **teal, frost=0.2, opal_color=srgb("#d6eeee")),
        "pane": glass_material(
            "amber-pane",
            color=srgb("#ffd690"),
            roughness=0.18,
            shadow=srgb("#ffe8bc"),
            glow=0.012,
        ),
        "core": emission_material("core-light", srgb("#ffe6b0"), 40.0),
        "backdrop": backdrop_material(),
        "outline": staging_material("logo-frame", srgb("#082b43")),
    }


# ---------------------------------------------------------------- scene


def screen_point(u: float, v: float, depth: float) -> Vector:
    """3D point drawn at SVG (u, v), pushed `depth` units behind the hero target."""
    forward = Vector((-math.cos(PHI) / math.sqrt(2), -math.cos(PHI) / math.sqrt(2), -math.sin(PHI)))
    right = Vector((-1.0, 1.0, 0.0)) / math.sqrt(2)
    up = Vector((-math.sin(PHI) / math.sqrt(2), -math.sin(PHI) / math.sqrt(2), math.cos(PHI)))
    return HERO_TARGET + forward * depth + right * ((u - 512.0) / PX_PER_UNIT) - up * ((v - 512.0) / PX_PER_UNIT)


def screen_polygon(
    name: str,
    points: list[tuple[float, float]],
    depth: float,
    material: bpy.types.Material,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """A flat polygon placed in screen space behind the hero camera's subject."""
    bm = bmesh.new()
    face = bm.faces.new([bm.verts.new(screen_point(u, v, depth)) for u, v in points])
    uv = bm.loops.layers.uv.new("UVMap")
    for loop, (u, v) in zip(face.loops, points, strict=True):
        loop[uv].uv = (u / 1024.0, 1.0 - v / 1024.0)
    bmesh.ops.recalc_face_normals(bm, faces=[face])
    obj = mesh_object(name, bm, material, collection)
    obj.visible_diffuse = False
    obj.visible_shadow = False
    return obj


def camera_only(obj: bpy.types.Object) -> None:
    """Show an object to the camera but hide it from reflections and refraction."""
    obj.visible_glossy = False
    obj.visible_transmission = False
    obj.visible_volume_scatter = False


def add_camera(name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """Create a camera object."""
    camera = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    collection.objects.link(camera)
    return camera


def add_light(
    name: str,
    kind: str,
    collection: bpy.types.Collection,
    *,
    location: tuple[float, float, float] | Vector,
    energy: float,
    color: tuple = (1.0, 1.0, 1.0),
    target: tuple[float, float, float] | Vector = (0.45, 0.45, -0.2),
    **settings: object,
) -> bpy.types.Object:
    """Create a light pointing at `target`, by default the logo's center."""
    light = bpy.data.lights.new(name, kind)
    light.energy = energy
    light.color = color[:3]
    for key, value in settings.items():
        setattr(light, key, value)
    obj = bpy.data.objects.new(name, light)
    obj.location = location
    direction = Vector(target) - Vector(location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    collection.objects.link(obj)
    return obj


def link_lights(
    light: bpy.types.Object,
    receivers: list[bpy.types.Object],
    name: str,
    *,
    exclude: bool = False,
) -> None:
    """Restrict a light to the given objects, or keep it off them, with Cycles light linking."""
    collection = bpy.data.collections.new(name)
    for obj in receivers:
        collection.objects.link(obj)
    if exclude:
        for link in collection.collection_objects:
            link.light_linking.link_state = "EXCLUDE"
    light.light_linking.receiver_collection = collection


def new_collection(name: str, parent: bpy.types.Collection | None = None) -> bpy.types.Collection:
    """Create and link a collection."""
    collection = bpy.data.collections.new(name)
    (parent or bpy.context.scene.collection).children.link(collection)
    return collection


def build_lights(frame: bpy.types.Object, wings: list[bpy.types.Object]) -> list[bpy.types.Object]:
    """Light the logo like the SVG and return the lights that move with the model."""
    lights = new_collection("lights")
    core = add_light(
        "core",
        "POINT",
        lights,
        location=CUBE_CENTER,
        energy=550.0,
        color=srgb("#ffd890"),
        shadow_soft_size=0.25,
    )
    core.visible_transmission = False  # Glow through the panes, not a visible bulb.
    # Overhead key lights the roofs and grazes the sides, keeping both wings symmetric.
    key = add_light("key", "AREA", lights, location=(0.45, 0.45, 7.0), energy=1500.0, color=srgb("#c4eef0"), size=6.0)
    # The roof faces mirror this softbox; it only shows in reflections so it cannot tint the glass.
    sky = add_light("sky", "AREA", lights, location=(-4.0, -4.0, 6.0), energy=900.0, color=srgb("#9adbee"), size=7.0)
    sky.visible_diffuse = sky.visible_transmission = sky.visible_volume_scatter = False
    # Only the core lights the amber panes, so the cube stays evenly gold like the SVG.
    link_lights(key, [*wings, frame], "key-receivers")
    link_lights(sky, wings, "sky-receivers")
    # The towers stand close behind the cube, in the shadow of its corner beams, so each back pane
    # also glows outward onto them as a warm panel.
    towers = [obj for obj in wings if obj.name.endswith("-tower")]
    # The SVG keeps the lower blocks teal; only their edges catch the gold.
    link_lights(core, [obj for obj in wings if obj not in towers], "core-excluded", exclude=True)
    spills = []
    for side, outward in [("left", Vector((0.0, -1.0, 0.0))), ("right", Vector((-1.0, 0.0, 0.0)))]:
        location = CUBE_CENTER + 0.52 * outward
        spill = add_light(
            f"spill-{side}",
            "AREA",
            lights,
            location=location,
            target=location + outward,
            energy=350.0,
            color=srgb("#ffc060"),
            size=1.0 - 2 * CUBE_BEAM,
        )
        # Seen through the glass, the panel itself would read as a bright card; it only lights faces.
        spill.visible_glossy = spill.visible_transmission = spill.visible_volume_scatter = False
        link_lights(spill, towers, f"spill-{side}-receivers")
        spills.append(spill)
    return [core, *spills]


def build_hero_set(materials: dict[str, bpy.types.Material]) -> None:
    """The SVG's orthographic camera, background, and navy outline."""
    hero = new_collection("hero-set")
    camera = add_camera("hero-camera", hero)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 1024.0 / PX_PER_UNIT
    camera.data.clip_end = 100.0
    camera.rotation_euler = (math.pi / 2 - PHI, 0.0, math.radians(135.0))
    camera.location = screen_point(512.0, 512.0, -30.0)
    margin = [(-400.0, -400.0), (1424.0, -400.0), (1424.0, 1424.0), (-400.0, 1424.0)]
    screen_polygon("hero-backdrop", margin, 8.0, materials["backdrop"], hero)
    # The SVG's navy outline is staging for the hero shot, not part of the model.
    camera_only(screen_polygon("hero-logo-frame", STRUCTURAL_FRAME, 7.9, materials["outline"], hero))


def configure_render(scene: bpy.types.Scene) -> None:
    """Cycles settings for clean glass, plus the view transform and bloom."""
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.008
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 32
    scene.cycles.transmission_bounces = 32
    scene.cycles.transparent_max_bounces = 32
    scene.cycles.glossy_bounces = 8
    scene.cycles.diffuse_bounces = 3
    scene.cycles.volume_bounces = 2
    scene.cycles.caustics_reflective = False
    scene.cycles.caustics_refractive = False
    scene.cycles.blur_glossy = 0.5
    # PBR Neutral keeps brand colors such as the backdrop nearly exact while rolling off the core.
    scene.view_settings.view_transform = "Khronos PBR Neutral"
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    build_compositor(scene)


def build_scene() -> None:
    """Assemble the logo, lights, the hero camera set, and render settings."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    materials = build_materials()
    logo = new_collection("mindroom-logo")
    cutters = new_collection("cutters", logo)
    frame, _panes, _core = build_cube(materials, logo, cutters)
    wings = [
        obj
        for name, mirror in [("left", False), ("right", True)]
        for obj in build_wing(name, materials, logo, cutters, mirror=mirror)
    ]
    build_lights(frame, wings)
    build_hero_set(materials)
    scene.world = build_world()
    scene.camera = bpy.data.objects["hero-camera"]
    configure_render(scene)


def build_world() -> bpy.types.World:
    """A teal sky: glass mirrors a bright zenith on its roofs and a navy floor below."""
    world = bpy.data.worlds.new("teal-sky")
    tree = world.node_tree
    nodes, links = tree.nodes, tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    separate = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Generated"], separate.inputs["Vector"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "EASE"
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = srgb("#061626")
    ramp.color_ramp.elements[1].position = 1.0
    ramp.color_ramp.elements[1].color = srgb("#9adbee")
    for position, color in [(0.5, "#0f3a5e"), (0.8, "#1a5288")]:
        ramp.color_ramp.elements.new(position).color = srgb(color)
    remap = nodes.new("ShaderNodeMapRange")
    remap.inputs["From Min"].default_value = -1.0
    links.new(separate.outputs["Z"], remap.inputs["Value"])
    links.new(remap.outputs["Result"], ramp.inputs["Fac"])
    background = nodes["Background"]
    background.inputs["Strength"].default_value = 2.6
    links.new(ramp.outputs["Color"], background.inputs["Color"])
    return world


def build_compositor(scene: bpy.types.Scene) -> None:
    """Bloom around the glowing core, like the soft light in the SVG."""
    tree = bpy.data.node_groups.new("glow", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    layers = tree.nodes.new("CompositorNodeRLayers")
    glare = tree.nodes.new("CompositorNodeGlare")
    glare.inputs["Type"].default_value = "Bloom"
    glare.inputs["Threshold"].default_value = 0.9
    glare.inputs["Strength"].default_value = 0.35
    glare.inputs["Size"].default_value = 0.6
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(layers.outputs["Image"], glare.inputs["Image"])
    tree.links.new(glare.outputs["Image"], output.inputs["Image"])
    scene.compositing_node_group = tree
    scene.render.use_compositing = True


def render(output_dir: Path, resolution: int, samples: int) -> None:
    """Render the hero still."""
    scene = bpy.context.scene
    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.cycles.samples = samples
    scene.render.filepath = str(output_dir / "hero.png")
    bpy.ops.render.render(write_still=True)


def main() -> None:
    """Parse arguments after Blender's `--`, build, save, and render."""
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="Render the hero still after saving the scene.")
    parser.add_argument("--resolution", type=int, default=1024)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--blend", type=Path, default=HERE / "mindroom-logo.blend")
    args = parser.parse_args(argv)

    build_scene()
    print(f"cube height {H:.4f}, beam {CUBE_BEAM:.4f}, tower wall {TOWER_WALL:.4f}, foot wall {FOOT_WALL:.4f}")
    print(f"wing x {WING_X}, tower y {TOWER_Y}, tower bottom {TOWER_BOTTOM:.4f}, foot z {FOOT_Z}")
    print(f"well x {WELL_X}, well y {WELL_Y:.4f}, bridge depth {BRIDGE_DEPTH:.4f}")
    args.blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.context.preferences.filepaths.save_version = 0  # No .blend1 backup beside the source.
    bpy.ops.wm.save_as_mainfile(filepath=str(args.blend), compress=True)
    if args.render:
        render(args.output_dir, args.resolution, args.samples)


if __name__ == "__main__":
    main()
