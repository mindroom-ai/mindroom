# ruff: noqa: INP001 -- Standalone Blender script, outside the application package.

"""Render the MindRoom M as luminous crystal around a glowing tesseract.

Run from the repository root:

    blender --background --factory-startup --python assets/logo/blender/crystal.py -- --render still

The geometry comes from model.py. This script dresses it in crystal, sets each
leg in a navy frame around a soft white light, turns the central cube into a
hypercube (an inner cube joined to the frame by struts, projected from real 4D
vertices), stages it on a dark mirror floor, and renders a still or one of four
effects as numbered PNG frames for ffmpeg.
"""

import argparse
import itertools
import math
import sys
import urllib.request
from pathlib import Path

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import model as logo  # noqa: E402

FRAMES = 120  # Four seconds at 30 fps.
LOCK_IN_HOLD = 30  # Frames held on the finished logo after the camera locks in.
LOCK_FRAME = FRAMES - LOCK_IN_HOLD  # The camera arrives, the M forms, and a flash bursts from the center.
REVEAL_FRAMES = LOCK_FRAME + 75  # The reveal's glide, its flash, and a second of rest: five and a half seconds.
EFFECTS = ("still", "wallpaper", "lock-in", "ignition", "hyperspin", "reveal")
HDRI = "studio_small_09"  # CC0 studio lighting from Poly Haven, used for reflections.
HDRI_URL = f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/{HDRI}_2k.hdr"
HDRI_CACHE = Path.home() / ".cache" / "mindroom-logo" / "hdri" / f"{HDRI}_2k.hdr"
NAVY = "#050d16"
GOLD = "#ffc566"
# A 4D viewer at distance 4 draws the far cell at 3/5 the size of the near one.
VIEW_DISTANCE_4D = 4.0
FILAMENT = {"inner": 24.0, "strut": 8.0, "outer": 8.0}  # Emission strengths per edge family.
LEG_GLOW = 10.0  # Emission strength at the center of each leg's white core.


def srgb(hex_color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    """Convert an sRGB hex color to linear RGBA."""
    channels = [int(hex_color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return (*linear, alpha)


def node_material(name: str) -> tuple[bpy.types.Material, bpy.types.NodeTree]:
    """Create a node material that holds only an output node."""
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    tree.nodes.clear()
    tree.nodes.new("ShaderNodeOutputMaterial")
    return material, tree


def output(tree: bpy.types.NodeTree) -> bpy.types.Node:
    """The material output node."""
    return next(node for node in tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial")


def crystal_glass(*, frozen: bool) -> bpy.types.Material:
    """Solid azure crystal that glows from within: a surface tint plus faint internal scattering."""
    material, tree = node_material("azure-crystal")
    nodes, links = tree.nodes, tree.links
    glass = nodes.new("ShaderNodeBsdfPrincipled")
    glass.inputs["Base Color"].default_value = srgb("#6cc6ee")
    glass.inputs["Roughness"].default_value = 0.0
    glass.inputs["IOR"].default_value = 1.52
    glass.inputs["Transmission Weight"].default_value = 1.0
    links.new(glass.outputs["BSDF"], output(tree).inputs["Surface"])
    absorb = nodes.new("ShaderNodeVolumeAbsorption")
    absorb.inputs["Color"].default_value = srgb("#40e1f5")
    absorb.inputs["Density"].default_value = 1.0
    scatter = nodes.new("ShaderNodeVolumeScatter")
    scatter.inputs["Color"].default_value = srgb("#5ec8f0")
    scatter.inputs["Density"].default_value = 0.6
    scatter.inputs["Anisotropy"].default_value = 0.4
    volume = nodes.new("ShaderNodeAddShader")
    links.new(absorb.outputs["Volume"], volume.inputs[0])
    links.new(scatter.outputs["Volume"], volume.inputs[1])
    links.new(volume.outputs["Shader"], output(tree).inputs["Volume"])
    if frozen:
        frost(tree, glass, scatter)
        material.cycles.volume_step_rate = 0.25  # The thin fracture planes need finer volume steps.
    return material


def frost(tree: bpy.types.NodeTree, glass: bpy.types.Node, scatter: bpy.types.Node) -> None:
    """Turn the crystal into ice: a paler tint, patchy frost, a hammered surface, and fracture planes.

    Every ingredient is weighted by the thaw mask, so a front sweeping out from the cube melts the ice back
    into the clear crystal; by default the front sits far inside the cube and everything stays frozen.
    """
    nodes, links = tree.nodes, tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    ice = thaw_mask(tree, coords)
    tint = nodes.new("ShaderNodeMix")
    tint.data_type = "RGBA"
    tint.inputs["A"].default_value = glass.inputs["Base Color"].default_value
    tint.inputs["B"].default_value = srgb("#a6dcf2")
    links.new(ice, tint.inputs["Factor"])
    links.new(tint.outputs["Result"], glass.inputs["Base Color"])
    patches = nodes.new("ShaderNodeTexNoise")
    patches.inputs["Scale"].default_value = 4.0
    patches.inputs["Detail"].default_value = 6.0
    links.new(coords.outputs["Object"], patches.inputs["Vector"])
    roughness = nodes.new("ShaderNodeMapRange")
    roughness.inputs["From Min"].default_value = 0.4
    roughness.inputs["From Max"].default_value = 0.7
    roughness.inputs["To Min"].default_value = 0.02
    roughness.inputs["To Max"].default_value = 0.16
    links.new(patches.outputs["Fac"], roughness.inputs["Value"])
    links.new(scaled(tree, roughness.outputs["Result"], ice), glass.inputs["Roughness"])
    grain = nodes.new("ShaderNodeTexNoise")
    grain.inputs["Scale"].default_value = 18.0
    grain.inputs["Detail"].default_value = 4.0
    links.new(coords.outputs["Object"], grain.inputs["Vector"])
    relief = nodes.new("ShaderNodeBump")
    relief.inputs["Distance"].default_value = 0.02
    links.new(scaled(tree, ice, 0.06), relief.inputs["Strength"])
    links.new(grain.outputs["Fac"], relief.inputs["Height"])
    links.new(relief.outputs["Normal"], glass.inputs["Normal"])
    # Fractures are thin sheets of dense white scattering along the edges of large Voronoi cells.
    cells = nodes.new("ShaderNodeTexVoronoi")
    cells.feature = "DISTANCE_TO_EDGE"
    cells.inputs["Scale"].default_value = 2.5
    links.new(coords.outputs["Object"], cells.inputs["Vector"])
    fractures = nodes.new("ShaderNodeMapRange")
    fractures.inputs["From Min"].default_value = 0.0
    fractures.inputs["From Max"].default_value = 0.025
    fractures.inputs["To Min"].default_value = 35.0
    fractures.inputs["To Max"].default_value = 0.0
    links.new(cells.outputs["Distance"], fractures.inputs["Value"])
    density = nodes.new("ShaderNodeMath")
    density.inputs[1].default_value = scatter.inputs["Density"].default_value
    links.new(scaled(tree, fractures.outputs["Result"], ice), density.inputs[0])
    links.new(density.outputs["Value"], scatter.inputs["Density"])
    haze = nodes.new("ShaderNodeMix")
    haze.data_type = "RGBA"
    haze.inputs["A"].default_value = scatter.inputs["Color"].default_value
    haze.inputs["B"].default_value = srgb("#f2fbff")
    links.new(ice, haze.inputs["Factor"])
    links.new(haze.outputs["Result"], scatter.inputs["Color"])


def thaw_mask(tree: bpy.types.NodeTree, coords: bpy.types.Node) -> bpy.types.NodeSocket:
    """1 where the ice still stands, 0 inside the "frost-front" radius around the cube, with a soft edge."""
    nodes, links = tree.nodes, tree.links
    front = nodes.new("ShaderNodeValue")
    front.name = "frost-front"
    front.outputs["Value"].default_value = -100.0  # Far inside the cube: nothing has melted.
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    offset.inputs[1].default_value = logo.CUBE_CENTER
    links.new(coords.outputs["Object"], offset.inputs[0])
    distance = nodes.new("ShaderNodeVectorMath")
    distance.operation = "LENGTH"
    links.new(offset.outputs["Vector"], distance.inputs[0])
    beyond = nodes.new("ShaderNodeMath")
    beyond.operation = "SUBTRACT"
    links.new(distance.outputs["Value"], beyond.inputs[0])
    links.new(front.outputs["Value"], beyond.inputs[1])
    edge = nodes.new("ShaderNodeMapRange")
    edge.interpolation_type = "SMOOTHSTEP"
    edge.inputs["From Min"].default_value = -0.3
    edge.inputs["From Max"].default_value = 0.0
    links.new(beyond.outputs["Value"], edge.inputs["Value"])
    return edge.outputs["Result"]


def scaled(
    tree: bpy.types.NodeTree,
    value: bpy.types.NodeSocket,
    factor: bpy.types.NodeSocket | float,
) -> bpy.types.NodeSocket:
    """A math node multiplying `value` by a socket or a constant."""
    product = tree.nodes.new("ShaderNodeMath")
    product.operation = "MULTIPLY"
    tree.links.new(value, product.inputs[0])
    if isinstance(factor, float):
        product.inputs[1].default_value = factor
    else:
        tree.links.new(factor, product.inputs[1])
    return product.outputs["Value"]


def lacquer() -> bpy.types.Material:
    """Glossy navy for the outer cube frame: dark, with crisp highlights on its edges."""
    material, tree = node_material("navy-lacquer")
    paint = tree.nodes.new("ShaderNodeBsdfPrincipled")
    paint.inputs["Base Color"].default_value = srgb("#0b2a45")
    paint.inputs["Roughness"].default_value = 0.15
    tree.links.new(paint.outputs["BSDF"], output(tree).inputs["Surface"])
    return material


def emitter(name: str, color: str, strength: float, *, transparent: bool = False) -> bpy.types.Material:
    """Unlit glow; transparent emitters let what lies behind show through."""
    material, tree = node_material(name)
    glow = tree.nodes.new("ShaderNodeEmission")
    glow.inputs["Color"].default_value = srgb(color)
    glow.inputs["Strength"].default_value = strength
    surface = glow.outputs["Emission"]
    if transparent:
        clear = tree.nodes.new("ShaderNodeBsdfTransparent")
        both = tree.nodes.new("ShaderNodeAddShader")
        tree.links.new(clear.outputs["BSDF"], both.inputs[0])
        tree.links.new(surface, both.inputs[1])
        surface = both.outputs["Shader"]
    tree.links.new(surface, output(tree).inputs["Surface"])
    return material


def mirror_floor() -> bpy.types.Material:
    """Glossy navy floor that fades into the background toward the horizon."""
    material, tree = node_material("mirror-floor")
    nodes, links = tree.nodes, tree.links
    paint = nodes.new("ShaderNodeBsdfPrincipled")
    paint.inputs["Base Color"].default_value = srgb("#071420")
    paint.inputs["Roughness"].default_value = 0.15
    far = nodes.new("ShaderNodeEmission")
    far.inputs["Color"].default_value = srgb(NAVY)
    coords = nodes.new("ShaderNodeTexCoord")
    distance = nodes.new("ShaderNodeVectorMath")
    distance.operation = "LENGTH"
    links.new(coords.outputs["Object"], distance.inputs[0])
    fade = nodes.new("ShaderNodeMapRange")
    fade.interpolation_type = "SMOOTHSTEP"
    fade.inputs["From Min"].default_value = 4.0
    fade.inputs["From Max"].default_value = 14.0
    links.new(distance.outputs["Value"], fade.inputs["Value"])
    mix = nodes.new("ShaderNodeMixShader")
    links.new(fade.outputs["Result"], mix.inputs["Fac"])
    links.new(paint.outputs["BSDF"], mix.inputs[1])
    links.new(far.outputs["Emission"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output(tree).inputs["Surface"])
    return material


def studio_world() -> bpy.types.World:
    """Studio HDRI for reflections and light; camera rays see plain navy instead."""
    if not HDRI_CACHE.exists():
        HDRI_CACHE.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(HDRI_URL, HDRI_CACHE)  # noqa: S310 -- Fixed https URL.
    world = bpy.data.worlds.new("studio")
    tree = world.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputWorld")
    coords = nodes.new("ShaderNodeTexCoord")
    turn = nodes.new("ShaderNodeMapping")
    turn.name = "hdri-turn"  # The lock-in turns the studio with the camera.
    links.new(coords.outputs["Generated"], turn.inputs["Vector"])
    environment = nodes.new("ShaderNodeTexEnvironment")
    environment.image = bpy.data.images.load(str(HDRI_CACHE))
    links.new(turn.outputs["Vector"], environment.inputs["Vector"])
    # The studio fades out toward the horizon: at grazing angles the glossy floor
    # would mirror its bright windows as white glare.
    height = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Generated"], height.inputs["Vector"])
    above = nodes.new("ShaderNodeMapRange")
    above.interpolation_type = "SMOOTHSTEP"
    above.inputs["From Min"].default_value = 0.3
    above.inputs["From Max"].default_value = 0.7
    links.new(height.outputs["Z"], above.inputs["Value"])
    overhead = nodes.new("ShaderNodeMix")
    overhead.data_type = "RGBA"
    overhead.blend_type = "MULTIPLY"
    overhead.inputs["Factor"].default_value = 1.0
    links.new(environment.outputs["Color"], overhead.inputs["A"])
    links.new(above.outputs["Result"], overhead.inputs["B"])
    lighting = nodes.new("ShaderNodeBackground")
    lighting.name = "studio-light"
    lighting.inputs["Strength"].default_value = 0.7
    links.new(overhead.outputs["Result"], lighting.inputs["Color"])
    backdrop = nodes.new("ShaderNodeBackground")
    backdrop.inputs["Color"].default_value = srgb(NAVY)
    path = nodes.new("ShaderNodeLightPath")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    links.new(lighting.outputs["Background"], mix.inputs[1])
    links.new(backdrop.outputs["Background"], mix.inputs[2])
    links.new(mix.outputs["Shader"], out.inputs["Surface"])
    return world


# ---------------------------------------------------------------- tesseract


def tesseract(angle: float) -> tuple[dict, list]:
    """Project a tesseract rotated by `angle` in the x-w plane into the cube's interior.

    Returns the projected vertices by 4D sign tuple and the 32 edges as
    (family, start, end), where the family is the edge's original cell.
    """
    w = logo.CUBE_BEAM
    half = Vector(((1 - 2 * w) / 2, (1 - 2 * w) / 2, (logo.H - 2 * w) / 2))
    near = VIEW_DISTANCE_4D / (VIEW_DISTANCE_4D - 1)
    points = {}
    for corner in itertools.product((-1, 1), repeat=4):
        x, y, z, depth = corner
        x, depth = x * math.cos(angle) - depth * math.sin(angle), x * math.sin(angle) + depth * math.cos(angle)
        scale = VIEW_DISTANCE_4D / (VIEW_DISTANCE_4D - depth) / near
        points[corner] = logo.CUBE_CENTER + Vector((x * half.x, y * half.y, z * half.z)) * scale
    edges = []
    for a, b in itertools.combinations(points, 2):
        differ = [i for i in range(4) if a[i] != b[i]]
        if len(differ) == 1:
            family = "strut" if differ[0] == 3 else ("inner" if a[3] < 0 else "outer")
            edges.append((family, a, b))
    return points, edges


def rod(bm: bmesh.types.BMesh, a: Vector, b: Vector, radius: float) -> None:
    """Add a capped cylinder from a to b."""
    direction = b - a
    if direction.length < 1e-6:
        return
    matrix = Matrix.Translation((a + b) / 2) @ direction.to_track_quat("Z", "Y").to_matrix().to_4x4()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        segments=16,
        radius1=radius,
        radius2=radius,
        depth=direction.length,
        matrix=matrix,
    )


def tesseract_meshes(
    angle: float,
    progress: dict[str, float] | None = None,
    *,
    inward: bool = False,
) -> dict[str, bmesh.types.BMesh]:
    """Filament meshes per edge family; `progress` draws each family partway, for tracing on.

    Struts trace outward from the inner cell, or inward from the frame when `inward` is set.
    """
    points, edges = tesseract(angle)
    progress = progress or {}
    meshes = {family: bmesh.new() for family in FILAMENT}
    radii = {"inner": 0.022, "strut": 0.013, "outer": 0.013}
    for family, a, b in edges:
        share = progress.get(family, 1.0)
        if share > 0:
            start, end = points[a], points[b]
            if family == "strut" and (a[3] > 0) != inward:
                start, end = end, start
            rod(meshes[family], start, start.lerp(end, share), radii[family])
    if progress.get("inner", 1.0) > 0:
        for corner, point in points.items():
            if corner[3] < 0:
                matrix = Matrix.Translation(point)
                bmesh.ops.create_uvsphere(meshes["inner"], u_segments=16, v_segments=8, radius=0.035, matrix=matrix)
    return meshes


def lantern_mesh(angle: float) -> bmesh.types.BMesh:
    """Faint glowing faces on the inner cell, so it reads as a solid cube inside the frame."""
    points, _edges = tesseract(angle)
    bm = bmesh.new()
    for corner, point in points.items():
        if corner[3] < 0:
            bm.verts.new(point)
    bmesh.ops.convex_hull(bm, input=bm.verts[:])
    return bm


def update_tesseract(angle: float, progress: dict[str, float] | None = None, *, inward: bool = False) -> None:
    """Rebuild the filament and lantern meshes in place."""
    for family, bm in tesseract_meshes(angle, progress, inward=inward).items():
        mesh = bpy.data.objects[f"tesseract-{family}"].data
        bm.to_mesh(mesh)
        bm.free()
        for polygon in mesh.polygons:
            polygon.use_smooth = True
    bm = lantern_mesh(angle)
    bm.to_mesh(bpy.data.objects["tesseract-lantern"].data)
    bm.free()


def build_tesseract(collection: bpy.types.Collection) -> None:
    """Create the filament families and the lantern, posed at angle 0."""
    for family, strength in FILAMENT.items():
        logo.mesh_object(f"tesseract-{family}", bmesh.new(), emitter(f"filament-{family}", GOLD, strength), collection)
    lantern_glow = emitter("lantern", "#ffb347", 0.6, transparent=True)
    lantern = logo.mesh_object("tesseract-lantern", bmesh.new(), lantern_glow, collection)
    lantern.visible_shadow = False
    update_tesseract(0.0)


# ---------------------------------------------------------------- legs


def leg_boxes() -> list[tuple[Vector, Vector]]:
    """Lowest and highest corners of the towers and feet, including the mirrored wing."""
    x0, x1 = logo.WING_X
    y0, y1 = logo.TOWER_Y
    boxes = []
    for bottom, top in ((logo.TOWER_BOTTOM, logo.H), logo.FOOT_Z):
        lo, hi = Vector((x0, y0, bottom)), Vector((x1, y1, top))
        boxes.append((lo, hi))
        boxes.append((Vector((lo.y, lo.x, lo.z)), Vector((hi.y, hi.x, hi.z))))  # The mirrored wing.
    return boxes


def leg_frames(collection: bpy.types.Collection) -> None:
    """Navy beams along every edge of the legs, so they read as glass set in frames like the cube."""
    beam, outset = 0.05, 0.006
    paint = bpy.data.materials["navy-lacquer"]
    for i, (low, high) in enumerate(leg_boxes()):
        lo, hi = low - Vector((outset,) * 3), high + Vector((outset,) * 3)
        bm = bmesh.new()
        logo.add_box(bm, lo, hi)
        frame = logo.mesh_object(f"leg-frame-{i}", bm, paint, collection)
        for axis in range(3):  # Hollowing the box through each axis leaves only its edges.
            cut_lo = [lo[k] + beam for k in range(3)]
            cut_hi = [hi[k] - beam for k in range(3)]
            cut_lo[axis], cut_hi[axis] = lo[axis] - 1.0, hi[axis] + 1.0
            bm = bmesh.new()
            logo.add_box(bm, cut_lo, cut_hi)
            logo.subtract(frame, logo.cutter(f"leg-frame-{i}-cut-{axis}", bm, collection))
        logo.bevel(frame, 0.01)


def leg_glow(collection: bpy.types.Collection) -> None:
    """A white light inside each leg: an emitting volume that fades from the center toward the faces."""
    material = leg_glow_material()
    inset = Vector((0.02, 0.02, 0.02))
    for i, (lo, hi) in enumerate(leg_boxes()):
        bm = bmesh.new()
        logo.add_box(bm, lo + inset, hi - inset)
        core = logo.mesh_object(f"leg-glow-{i}", bm, material, collection)
        core.visible_shadow = False


def leg_glow_material() -> bpy.types.Material:
    """Volume-only white emission, strongest at each leg's center and cut off above a fill level."""
    material, tree = node_material("leg-glow")
    nodes, links = tree.nodes, tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    offset.inputs[1].default_value = (0.5, 0.5, 0.5)
    links.new(coords.outputs["Generated"], offset.inputs[0])
    distance = nodes.new("ShaderNodeVectorMath")
    distance.operation = "LENGTH"
    links.new(offset.outputs["Vector"], distance.inputs[0])
    falloff = nodes.new("ShaderNodeMapRange")
    falloff.name = "glow-falloff"  # The ignition fades the glow in through its peak strength.
    falloff.interpolation_type = "SMOOTHSTEP"
    falloff.inputs["From Min"].default_value = 0.12
    falloff.inputs["From Max"].default_value = 0.5
    falloff.inputs["To Min"].default_value = LEG_GLOW
    falloff.inputs["To Max"].default_value = 0.0
    links.new(distance.outputs["Value"], falloff.inputs["Value"])
    glow = nodes.new("ShaderNodeEmission")
    glow.inputs["Color"].default_value = srgb("#f2fbff")
    links.new(fill_level(tree, coords, falloff.outputs["Result"]), glow.inputs["Strength"])
    links.new(glow.outputs["Emission"], output(tree).inputs["Volume"])
    return material


def fill_level(
    tree: bpy.types.NodeTree,
    coords: bpy.types.Node,
    strength: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    """Keep `strength` only below the "glow-level" height, plus a bright "glow-surface" band where it ends.

    This lets light rise in the legs like a liquid; at the default level, far above the letter, nothing changes.
    """
    nodes, links = tree.nodes, tree.links
    level = nodes.new("ShaderNodeValue")
    level.name = "glow-level"
    level.outputs["Value"].default_value = 100.0  # Far above the letter: fully lit.
    height = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Object"], height.inputs["Vector"])
    above = nodes.new("ShaderNodeMath")
    above.operation = "SUBTRACT"
    links.new(height.outputs["Z"], above.inputs[0])
    links.new(level.outputs["Value"], above.inputs[1])
    below = nodes.new("ShaderNodeMapRange")
    below.interpolation_type = "SMOOTHSTEP"
    below.inputs["From Min"].default_value = -0.12
    below.inputs["From Max"].default_value = 0.0
    below.inputs["To Min"].default_value = 1.0
    below.inputs["To Max"].default_value = 0.0
    links.new(above.outputs["Value"], below.inputs["Value"])
    filled = nodes.new("ShaderNodeMath")
    filled.operation = "MULTIPLY"
    links.new(strength, filled.inputs[0])
    links.new(below.outputs["Result"], filled.inputs[1])
    gap = nodes.new("ShaderNodeMath")
    gap.operation = "ABSOLUTE"
    links.new(above.outputs["Value"], gap.inputs[0])
    surface = nodes.new("ShaderNodeMapRange")
    surface.name = "glow-surface"
    surface.interpolation_type = "SMOOTHSTEP"
    surface.inputs["From Min"].default_value = 0.0
    surface.inputs["From Max"].default_value = 0.05
    surface.inputs["To Min"].default_value = 0.0
    surface.inputs["To Max"].default_value = 0.0
    links.new(gap.outputs["Value"], surface.inputs["Value"])
    total = nodes.new("ShaderNodeMath")
    links.new(filled.outputs["Value"], total.inputs[0])
    links.new(surface.outputs["Result"], total.inputs[1])
    return total.outputs["Value"]


# ---------------------------------------------------------------- scene


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


def core_light(feet: list[bpy.types.Object], frame: bpy.types.Object) -> bpy.types.Object:
    """A warm point light inside the cube's bead that lights the crystal around it."""
    core = add_light(
        "core",
        "POINT",
        logo.new_collection("lights"),
        location=logo.CUBE_CENTER,
        energy=700.0,
        color=srgb("#ffd890"),
        shadow_soft_size=0.25,
    )
    core.visible_transmission = False  # The bead shows where it is; the light itself stays unseen.
    core.visible_glossy = False  # The glass reflects the filaments, not a hot spot.
    # The feet stay cool, and the beams stay dark silhouettes while the filaments carry the glow.
    link_lights(core, [*feet, frame], "core-excluded", exclude=True)
    return core


def configure_render(scene: bpy.types.Scene) -> None:
    """Cycles settings for clean glass, the view transform, and the compositor's bloom and flash glare."""
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.use_adaptive_sampling = True
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
    scene.view_settings.view_transform = (
        "Khronos PBR Neutral"  # Keeps the navy and gold true while rolling off highlights.
    )
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    glow = bpy.data.node_groups.new("glow", "CompositorNodeTree")
    glow.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    layers = glow.nodes.new("CompositorNodeRLayers")
    bloom = glow.nodes.new("CompositorNodeGlare")  # A soft bloom around the glowing filaments.
    bloom.inputs["Type"].default_value = "Bloom"
    bloom.inputs["Threshold"].default_value = 2.0
    bloom.inputs["Strength"].default_value = 0.35
    bloom.inputs["Size"].default_value = 0.6
    streaks = glow.nodes.new("CompositorNodeGlare")  # Lens streaks, muted until the lock-in flash.
    streaks.name = "flash-streaks"
    streaks.inputs["Type"].default_value = "Streaks"
    streaks.inputs["Threshold"].default_value = 3.0
    streaks.inputs["Strength"].default_value = 0.0
    beams = glow.nodes.new("CompositorNodeGlare")  # Rays bursting from the core, likewise.
    beams.name = "flash-beams"
    beams.inputs["Type"].default_value = "Sun Beams"
    beams.inputs["Threshold"].default_value = 1.5
    beams.inputs["Strength"].default_value = 0.0
    streaks.mute = beams.mute = True
    out = glow.nodes.new("NodeGroupOutput")
    glow.links.new(layers.outputs["Image"], bloom.inputs["Image"])
    glow.links.new(bloom.outputs["Image"], streaks.inputs["Image"])
    glow.links.new(streaks.outputs["Image"], beams.inputs["Image"])
    glow.links.new(beams.outputs["Image"], out.inputs["Image"])
    scene.compositing_node_group = glow
    scene.render.use_compositing = True


def studio(collection: bpy.types.Collection) -> dict[str, bpy.types.Object]:
    """Mirror floor, a high back light, a cool pool behind the letter, and rim strips."""
    floor_z = logo.FOOT_Z[0]
    bm = bmesh.new()
    logo.add_box(bm, (-30.0, -30.0, floor_z - 0.02), (30.0, 30.0, floor_z))
    floor = logo.mesh_object("mirror-floor", bm, mirror_floor(), collection)
    level = Vector((1.0, 1.0, 0.0)).normalized()  # Toward the hero camera, along the floor.
    right = Vector((-1.0, 1.0, 0.0)).normalized()
    center = Vector((0.3, 0.3, (floor_z + logo.H) / 2))
    rig = {}
    # High enough that the floor does not mirror it back at the camera.
    rig["back"] = add_light(
        "back", "AREA", collection, location=center - 2.0 * level + Vector((0, 0, 5.5)), target=center,
        energy=1600.0, color=srgb("#f4fbff"), size=2.0,
    )  # fmt: skip
    # The glass refracts this pool on the floor behind it and glows.
    pool = center - 1.6 * level
    rig["behind"] = add_light(
        "behind", "AREA", collection, location=pool + Vector((0, 0, 3.0)), target=pool - Vector((0, 0, 5)),
        energy=600.0, color=srgb("#bfeaf2"), size=2.5,
    )  # fmt: skip
    link_lights(rig["behind"], [floor], "behind-receivers")
    rig["behind"].visible_glossy = False  # The floor would mirror the panel itself as a bright blob in front of the M.
    for side, sign in [("left", -1), ("right", 1)]:
        rig[f"rim-{side}"] = add_light(
            f"rim-{side}", "AREA", collection, location=center - 2.0 * level + 3.5 * sign * right + Vector((0, 0, 1.0)),
            target=center, energy=900.0, color=srgb("#dff3ff"), shape="RECTANGLE", size=0.4, size_y=4.0,
        )  # fmt: skip
    # The floor mirrors the back light and the low rims as white streaks from some angles;
    # they only exist to light the glass.
    for name in ("back", "rim-left", "rim-right"):
        link_lights(rig[name], [floor], f"{name}-skips-floor", exclude=True)
    # The lights hang on a rig that the lock-in turns with the camera, so the look holds from any side.
    mount = bpy.data.objects.new("light-rig", None)
    collection.objects.link(mount)
    mount.location = (center.x, center.y, 0.0)
    for light in rig.values():
        light.visible_camera = False
        light.parent = mount
        light.location -= mount.location
    rig["mount"] = mount
    return rig


def flash_haze(collection: bpy.types.Collection, rig: dict[str, bpy.types.Object]) -> bpy.types.Object:
    """Air around the letter that only the core lights; during the flash it shows rays streaming from the center."""
    material, tree = node_material("flash-haze")
    nodes, links = tree.nodes, tree.links
    # The haze thins out away from the cube, so the light blooms at the center and the edges stay dark.
    coords = nodes.new("ShaderNodeTexCoord")
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    offset.inputs[1].default_value = logo.CUBE_CENTER
    links.new(coords.outputs["Object"], offset.inputs[0])
    distance = nodes.new("ShaderNodeVectorMath")
    distance.operation = "LENGTH"
    links.new(offset.outputs["Vector"], distance.inputs[0])
    thickness = nodes.new("ShaderNodeMapRange")
    thickness.name = "haze-density"  # The lock-in raises the peak density for the flash.
    thickness.interpolation_type = "SMOOTHSTEP"
    thickness.inputs["From Min"].default_value = 0.4
    thickness.inputs["From Max"].default_value = 2.2
    thickness.inputs["To Min"].default_value = 0.0
    thickness.inputs["To Max"].default_value = 0.0
    links.new(distance.outputs["Value"], thickness.inputs["Value"])
    haze = nodes.new("ShaderNodeVolumeScatter")
    haze.inputs["Color"].default_value = srgb("#fff3dc")
    haze.inputs["Anisotropy"].default_value = 0.5  # Scattering forward makes the rays brightest toward the camera.
    links.new(thickness.outputs["Result"], haze.inputs["Density"])
    links.new(haze.outputs["Volume"], output(tree).inputs["Volume"])
    bm = bmesh.new()
    logo.add_box(bm, (-2.5, -2.5, logo.FOOT_Z[0] + 0.01), (3.5, 3.5, logo.H + 2.5))
    box = logo.mesh_object("flash-haze", bm, material, collection)
    box.hide_render = True  # Shown only while the flash lasts.
    for name in ("back", "rim-left", "rim-right"):  # The behind light already reaches only the floor.
        receivers = rig[name].light_linking.receiver_collection
        receivers.objects.link(box)
        receivers.collection_objects[len(receivers.objects) - 1].light_linking.link_state = "EXCLUDE"
    return box


def build(*, frozen: bool = False) -> dict[str, bpy.types.Object]:
    """Build the model and stage it, in clear crystal or frosted ice."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    logo.CUBE_BEAM = 0.11  # Slimmer beams open the frame enough to see the inner cube.
    scene = bpy.context.scene
    frame, *wings = logo.build_model(crystal_glass(frozen=frozen), lacquer(), emitter("core-light", "#ffe6b0", 400.0))
    core = core_light([wing for wing in wings if wing.name.endswith("-foot")], frame)
    configure_render(scene)
    scene.world = studio_world()
    collection = logo.new_collection("crystal-set")
    build_tesseract(collection)
    leg_frames(collection)
    leg_glow(collection)
    rig = studio(collection)
    rig["core"] = core
    camera = logo.logo_camera("crystal-camera", collection)
    camera.data.clip_end = 300.0
    scene.camera = rig["camera"] = camera
    rig["haze"] = flash_haze(collection, rig)
    return rig


# ---------------------------------------------------------------- effects


def ease(s: float) -> float:
    """Smoothstep easing on [0, 1]."""
    s = min(max(s, 0.0), 1.0)
    return s * s * (3 - 2 * s)


def glide(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """Swing the camera from a low, wide, off-axis view into the logo's single viewpoint, arriving at LOCK_FRAME."""
    camera = rig["camera"]
    camera.data.type = "PERSP"
    camera.data.sensor_width = 36.0
    s = ease((frame - 1) / (LOCK_FRAME - 1))
    azimuth = math.radians(45.0 + 80.0 * (1 - s))
    elevation = 0.17 + (logo.PHI - 0.17) * s
    camera.data.lens = 45.0 * (1 - s) + 400.0 * s  # A dolly zoom toward orthographic; the framing holds.
    view = Vector(
        (math.cos(azimuth) * math.cos(elevation), math.sin(azimuth) * math.cos(elevation), math.sin(elevation)),
    )
    camera.location = logo.VIEW_TARGET + (1024.0 / logo.PX_PER_UNIT * camera.data.lens / 36.0) * view
    camera.rotation_euler = (-view).to_track_quat("-Z", "Y").to_euler()
    # The pool behind the letter is only for the glass to refract from the logo's angle;
    # seen from low and close it would sit on the floor in plain view.
    # The studio's reflections likewise come up as the glass turns toward the logo's view.
    rig["behind"].data.energy = 600.0 * s**3
    world = bpy.context.scene.world.node_tree.nodes
    world["studio-light"].inputs["Strength"].default_value = 0.7 * (0.25 + 0.75 * s**2)
    turn = azimuth - math.radians(45.0)
    rig["mount"].rotation_euler = (0.0, 0.0, turn)
    world["hdri-turn"].inputs["Rotation"].default_value = (0.0, 0.0, turn)


def shine(
    rig: dict[str, bpy.types.Object],
    *,
    core: float = 1.0,
    spark: float = 1.0,
    lantern: float = 1.0,
    filaments: float = 1.0,
    legs: float = 1.0,
    level: float = 100.0,
    surface: float = 0.0,
) -> None:
    """Set the core light, its bead, the lantern, the filaments, and the leg glow as multiples of their resting levels.

    The legs glow up to the height `level`, with a band of strength `surface` where the light meets the dark.
    """
    rig["core"].data.energy = 700.0 * core
    bpy.data.objects["cube-core"].hide_render = spark <= 0  # Unlit, the bead would show as a black dot.
    materials = bpy.data.materials
    materials["core-light"].node_tree.nodes["Emission"].inputs["Strength"].default_value = 400.0 * spark
    materials["lantern"].node_tree.nodes["Emission"].inputs["Strength"].default_value = 0.6 * lantern
    for family, strength in FILAMENT.items():
        materials[f"filament-{family}"].node_tree.nodes["Emission"].inputs["Strength"].default_value = (
            strength * filaments
        )
    glow = materials["leg-glow"].node_tree.nodes
    glow["glow-falloff"].inputs["To Min"].default_value = LEG_GLOW * legs
    glow["glow-level"].outputs["Value"].default_value = level
    glow["glow-surface"].inputs["To Min"].default_value = surface


def flash(rig: dict[str, bpy.types.Object], burst: float, *, reach: float = 2.2) -> None:
    """The camera's side of a burst of strength `burst`: rays in the haze, lens glare, and exposure.

    The haze thins out to nothing at `reach` units from the cube; growing it spreads the light outward.
    """
    quiet = burst < 0.02
    rig["haze"].hide_render = quiet
    haze = bpy.data.materials["flash-haze"].node_tree.nodes["haze-density"]
    haze.inputs["To Min"].default_value = 0.15 * burst
    haze.inputs["From Max"].default_value = reach
    scene = bpy.context.scene
    lens = scene.compositing_node_group.nodes
    lens["flash-streaks"].mute = lens["flash-beams"].mute = quiet
    lens["flash-streaks"].inputs["Strength"].default_value = 0.4 * burst
    lens["flash-beams"].inputs["Strength"].default_value = 0.5 * burst
    if not quiet:
        bpy.context.view_layer.update()  # The beams need the camera's new pose to find the core on screen.
        core = world_to_camera_view(scene, rig["camera"], logo.CUBE_CENTER)
        lens["flash-beams"].inputs["Sun Position"].default_value = (core.x, core.y)
    scene.view_settings.exposure = 0.3 * burst


def lock_in(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """Glide into the logo's single viewpoint, then flash from the center as the M forms."""
    glide(rig, frame)
    since = frame - LOCK_FRAME
    burst = math.exp(-since / 4) if since >= 0 else 0.0  # Instant, gone within about half a second.
    flash(rig, burst)
    shine(rig, core=1 + 6 * burst, spark=1 + 6 * burst, filaments=1 + 3 * burst, legs=1 + 2 * burst)


def ignition(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """A spark at the center, the inner cube traces on, struts reach the frame, light fills in, and the legs wake."""
    light = ease((frame - 50) / 40)
    shine(rig, core=light, spark=ease((frame - 10) / 12), lantern=light, legs=ease((frame - 72) / 30))
    progress = {
        "inner": ease((frame - 22) / 30),
        "strut": ease((frame - 46) / 24),
        "outer": ease((frame - 64) / 20),
    }
    update_tesseract(0.0, progress)


def hyperspin(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """One full turn in the x-w plane: the inner cube passes through the outer one and back, looping."""
    del rig
    update_tesseract(2 * math.pi * (frame - 1) / FRAMES)


def reveal(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """While the camera swings in, light fills the frozen legs and converges on the core; it flashes as the M forms.

    Shortly after the glide starts, light rises in the unlit ice from the floor up, crosses into the cube
    as the outer cube traces on and the struts grow inward, and closes the inner cube just as the camera
    arrives. The flash then swells, holds while its rays spread outward, and melts the ice into clear
    crystal from the cube outward as it settles. Build the scene frozen for this effect.
    """
    glide(rig, frame)
    fill = ease((frame - 7) / 48)
    since = frame - LOCK_FRAME
    # The light arrives at the frame a second before the lock and closes the inner cube just in time.
    progress = {"outer": ease((since + 36) / 18), "strut": ease((since + 24) / 14), "inner": ease((since + 14) / 12)}
    if since < 0:
        burst = 0.0
    elif since < 3:
        burst = ease(since / 3)  # A quick swell rather than a cut.
    elif since < 12:
        burst = 1.0
    else:
        burst = math.exp(-(since - 12) / 12)
    flash(rig, 0.55 * burst, reach=0.8 + 0.05 * max(since, 0))
    lit = ease(since / 6)
    shine(
        rig,
        core=lit + 3 * burst,
        spark=lit + 5 * burst,
        lantern=lit,
        filaments=1 + 3 * burst,
        legs=1 + 2 * burst,
        level=logo.FOOT_Z[0] + fill * (logo.H - logo.FOOT_Z[0] + 0.2),
        surface=4.0 * (1 - lit) if fill > 0 else 0.0,
    )
    update_tesseract(0.0, progress, inward=True)
    # The flash melts the ice: a thaw front spreads from the cube through the legs into clear crystal.
    front = 3.4 * ease(since / 30) if since >= 0 else -100.0  # Past the farthest foot corner, 2.94 out.
    bpy.data.materials["azure-crystal"].node_tree.nodes["frost-front"].outputs["Value"].default_value = front


def widescreen(scene: bpy.types.Scene, camera: bpy.types.Object, width: int, height: int) -> None:
    """Frame the logo's view for a wide desktop: the M at half the height, a little above center.

    The framing holds for any aspect ratio; a wider screen only adds dark space at the sides.
    """
    scene.render.resolution_x, scene.render.resolution_y = width, height
    camera.data.sensor_fit = "VERTICAL"
    camera.data.ortho_scale *= 1.2
    camera.data.shift_y = 0.006  # Puts the cube's center at 54% of the height.


def render(
    effect: str,
    output_dir: Path,
    resolution: int,
    samples: int,
    *,
    frozen: bool = False,
    height: int | None = None,
) -> None:
    """Build and render one effect: a still PNG, or numbered frames in a folder.

    The wallpaper is the still framed for a desktop `resolution` pixels wide and `height` tall, 16:9 by default.
    """
    rig = build(frozen=frozen or effect == "reveal")  # The reveal starts in ice and melts.
    scene = bpy.context.scene
    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.cycles.samples = samples
    scene.cycles.adaptive_threshold = 0.02  # The denoiser cleans up the rest; frames stay affordable.
    scene.render.use_persistent_data = True  # Frames reuse the scene and resync only what changed.
    if effect in ("still", "wallpaper"):
        scene.render.image_settings.color_depth = "16"  # The dark gradients band at 8 bits per channel.
        if effect == "wallpaper":
            widescreen(scene, rig["camera"], resolution, height or resolution * 9 // 16)
        scene.render.filepath = str(output_dir / ("crystal.png" if effect == "still" else "crystal-wallpaper.png"))
        bpy.ops.render.render(write_still=True)
        return
    step, length = {
        "lock-in": (lock_in, FRAMES),
        "ignition": (ignition, FRAMES),
        "hyperspin": (hyperspin, FRAMES),
        "reveal": (reveal, REVEAL_FRAMES),
    }[effect]
    for frame in range(1, length + 1):
        step(rig, frame)
        scene.render.filepath = str(output_dir / effect / f"frame-{frame:04d}.png")
        bpy.ops.render.render(write_still=True)


def main() -> None:
    """Parse arguments after Blender's `--`, then render each requested effect."""
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", nargs="+", default=["still"], choices=EFFECTS)
    parser.add_argument("--resolution", type=int, default=1080)
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--blend", type=Path, help="Also save the scene of the last effect here.")
    parser.add_argument("--frozen", action="store_true", help="Frosted, cracked ice instead of clear crystal.")
    parser.add_argument("--height", type=int, help="Wallpaper height in pixels; 16:9 to the resolution by default.")
    args = parser.parse_args(argv)
    for effect in args.render:
        render(effect, args.output_dir, args.resolution, args.samples, frozen=args.frozen, height=args.height)
    if args.blend:
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(args.blend), compress=True)


if __name__ == "__main__":
    main()
