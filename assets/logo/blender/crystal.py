# ruff: noqa: INP001 -- Standalone Blender script, outside the application package.

"""Render the MindRoom M as luminous crystal around a glowing tesseract.

Run from the repository root:

    blender --background --factory-startup --python assets/logo/blender/crystal.py -- --render still

The geometry comes from build_scene.py. This script makes the glass solid, sets
each leg in a navy frame around a soft white light, turns the central cube into a
hypercube (an inner cube joined to the frame by struts, projected from real 4D
vertices), stages it on a dark mirror floor, and renders a still or one of three
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
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_scene as logo  # noqa: E402

FRAMES = 120  # Four seconds at 30 fps.
LOCK_IN_HOLD = 30  # Frames held on the finished logo after the camera locks in.
# The lock-in keeps the drawing's full depth so the parts visibly gather;
# the other shots use the compact model so floor reflections stay close to the letter.
EFFECTS = {"still": 0.9, "lock-in": 0.0, "ignition": 0.9, "hyperspin": 0.9}
HDRI = "studio_small_09"  # CC0 studio lighting from Poly Haven, used for reflections.
HDRI_URL = f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/{HDRI}_2k.hdr"
HDRI_CACHE = Path.home() / ".cache" / "mindroom-logo" / "hdri" / f"{HDRI}_2k.hdr"
NAVY = "#050d16"
GOLD = "#ffc566"
# A 4D viewer at distance 4 draws the far cell at 3/5 the size of the near one.
VIEW_DISTANCE_4D = 4.0
FILAMENT = {"inner": 24.0, "strut": 8.0, "outer": 8.0}  # Emission strengths per edge family.
LEG_GLOW = 10.0  # Emission strength at the center of each leg's white core.


def node_material(name: str) -> tuple[bpy.types.Material, bpy.types.NodeTree]:
    """Create an empty node material with an output node."""
    material, tree = logo.node_material(name)
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
    glass.inputs["Base Color"].default_value = logo.srgb("#6cc6ee")
    glass.inputs["Roughness"].default_value = 0.0
    glass.inputs["IOR"].default_value = 1.52
    glass.inputs["Transmission Weight"].default_value = 1.0
    links.new(glass.outputs["BSDF"], output(tree).inputs["Surface"])
    absorb = nodes.new("ShaderNodeVolumeAbsorption")
    absorb.inputs["Color"].default_value = logo.srgb("#40e1f5")
    absorb.inputs["Density"].default_value = 1.0
    scatter = nodes.new("ShaderNodeVolumeScatter")
    scatter.inputs["Color"].default_value = logo.srgb("#5ec8f0")
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
    """Turn the crystal into ice: a paler tint, patchy frost, a hammered surface, and fracture planes."""
    nodes, links = tree.nodes, tree.links
    glass.inputs["Base Color"].default_value = logo.srgb("#a6dcf2")
    coords = nodes.new("ShaderNodeTexCoord")
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
    links.new(roughness.outputs["Result"], glass.inputs["Roughness"])
    grain = nodes.new("ShaderNodeTexNoise")
    grain.inputs["Scale"].default_value = 18.0
    grain.inputs["Detail"].default_value = 4.0
    links.new(coords.outputs["Object"], grain.inputs["Vector"])
    relief = nodes.new("ShaderNodeBump")
    relief.inputs["Strength"].default_value = 0.06
    relief.inputs["Distance"].default_value = 0.02
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
    links.new(fractures.outputs["Result"], density.inputs[0])
    links.new(density.outputs["Value"], scatter.inputs["Density"])
    scatter.inputs["Color"].default_value = logo.srgb("#f2fbff")


def lacquer() -> bpy.types.Material:
    """Glossy navy for the outer cube frame: dark, with crisp highlights on its edges."""
    material, tree = node_material("navy-lacquer")
    paint = tree.nodes.new("ShaderNodeBsdfPrincipled")
    paint.inputs["Base Color"].default_value = logo.srgb("#0b2a45")
    paint.inputs["Roughness"].default_value = 0.15
    tree.links.new(paint.outputs["BSDF"], output(tree).inputs["Surface"])
    return material


def emitter(name: str, color: str, strength: float, *, transparent: bool = False) -> bpy.types.Material:
    """Unlit glow; transparent emitters let what lies behind show through."""
    material, tree = node_material(name)
    glow = tree.nodes.new("ShaderNodeEmission")
    glow.inputs["Color"].default_value = logo.srgb(color)
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
    paint.inputs["Base Color"].default_value = logo.srgb("#071420")
    paint.inputs["Roughness"].default_value = 0.15
    far = nodes.new("ShaderNodeEmission")
    far.inputs["Color"].default_value = logo.srgb(NAVY)
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
    backdrop.inputs["Color"].default_value = logo.srgb(NAVY)
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


def tesseract_meshes(angle: float, progress: dict[str, float] | None = None) -> dict[str, bmesh.types.BMesh]:
    """Filament meshes per edge family; `progress` draws each family partway, for tracing on."""
    points, edges = tesseract(angle)
    progress = progress or {}
    meshes = {family: bmesh.new() for family in FILAMENT}
    radii = {"inner": 0.022, "strut": 0.013, "outer": 0.013}
    for family, a, b in edges:
        share = progress.get(family, 1.0)
        if share > 0:
            start, end = points[a], points[b]
            if family == "strut" and a[3] > 0:
                start, end = end, start  # Struts grow outward from the inner cell.
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


def update_tesseract(angle: float, progress: dict[str, float] | None = None) -> None:
    """Rebuild the filament and lantern meshes in place."""
    for family, bm in tesseract_meshes(angle, progress).items():
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
    """Lowest and highest corners of the towers and feet, after the slide and the mirroring."""
    x0, x1 = logo.WING_X
    y0, y1 = logo.TOWER_Y
    shift = logo.TOWER_SLIDE * logo.TOWARD_CAMERA
    boxes = []
    for bottom, top in ((logo.TOWER_BOTTOM, logo.H), logo.FOOT_Z):
        lo, hi = Vector((x0, y0, bottom)) + shift, Vector((x1, y1, top)) + shift
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
    glow.inputs["Color"].default_value = logo.srgb("#f2fbff")
    links.new(falloff.outputs["Result"], glow.inputs["Strength"])
    links.new(glow.outputs["Emission"], output(tree).inputs["Volume"])
    inset = Vector((0.02, 0.02, 0.02))
    for i, (lo, hi) in enumerate(leg_boxes()):
        bm = bmesh.new()
        logo.add_box(bm, lo + inset, hi - inset)
        core = logo.mesh_object(f"leg-glow-{i}", bm, material, collection)
        core.visible_shadow = False


# ---------------------------------------------------------------- scene


def crystallize(scene: bpy.types.Scene, *, frozen: bool) -> None:
    """Swap the logo look for solid crystal, an open navy frame, and the tesseract's light."""
    for name in ("hero-set", "studio-set"):
        bpy.data.collections[name].hide_render = True
    for name in ("key", "sky", "spill-left", "spill-right"):
        bpy.data.objects.remove(bpy.data.objects[name])
    pivot = bpy.data.objects["logo-pivot"]  # Effects animate the camera and the tesseract, not the model.
    pivot.driver_remove("rotation_euler", 2)
    pivot.rotation_euler = (0.0, 0.0, 0.0)

    glass = crystal_glass(frozen=frozen)
    for side in ("left", "right"):
        for part in ("tower", "foot"):
            bpy.data.objects[f"{side}-{part}"].data.materials[0] = glass
    frame = bpy.data.objects["cube-frame"]
    frame.data.materials[0] = lacquer()
    frame.modifiers["Bevel"].width = 0.02
    frame.modifiers["Bevel"].segments = 4
    # The hypercube needs an open frame: panes would mirror the azure towers over the gold.
    bpy.data.objects["cube-panes"].hide_render = True
    # The beams stay dark silhouettes; the filaments carry the glow.
    excluded = bpy.data.collections["core-excluded"]
    excluded.objects.link(frame)
    excluded.collection_objects[len(excluded.objects) - 1].light_linking.link_state = "EXCLUDE"
    core = bpy.data.objects["core"]
    core.data.energy = 700.0
    core.visible_glossy = False  # It lights the crystal; the filaments are what the glass reflects.
    bpy.data.materials["core-light"].node_tree.nodes["Emission"].inputs["Strength"].default_value = 400.0
    scene.compositing_node_group.nodes["Glare"].inputs["Threshold"].default_value = 2.0
    scene.world = studio_world()


def studio(collection: bpy.types.Collection) -> dict[str, bpy.types.Object]:
    """Mirror floor, a high back light, a cool pool behind the letter, and rim strips."""
    floor_z = logo.floor_z()
    bm = bmesh.new()
    logo.add_box(bm, (-30.0, -30.0, floor_z - 0.02), (30.0, 30.0, floor_z))
    floor = logo.mesh_object("mirror-floor", bm, mirror_floor(), collection)
    toward = logo.TOWARD_CAMERA
    level = Vector((toward.x, toward.y, 0.0)).normalized()
    right = Vector((-1.0, 1.0, 0.0)).normalized()
    center = Vector((0.3, 0.3, (floor_z + logo.H) / 2))
    rig = {}
    # High enough that the floor does not mirror it back at the camera.
    rig["back"] = logo.add_light(
        "back", "AREA", collection, location=center - 2.0 * level + Vector((0, 0, 5.5)), target=center,
        energy=1600.0, color=logo.srgb("#f4fbff"), size=2.0,
    )  # fmt: skip
    # The glass refracts this pool on the floor behind it and glows.
    pool = center - 1.6 * level
    rig["behind"] = logo.add_light(
        "behind", "AREA", collection, location=pool + Vector((0, 0, 3.0)), target=pool - Vector((0, 0, 5)),
        energy=600.0, color=logo.srgb("#bfeaf2"), size=2.5,
    )  # fmt: skip
    logo.link_lights(rig["behind"], [floor], "behind-receivers")
    for side, sign in [("left", -1), ("right", 1)]:
        rig[f"rim-{side}"] = logo.add_light(
            f"rim-{side}", "AREA", collection, location=center - 2.0 * level + 3.5 * sign * right + Vector((0, 0, 1.0)),
            target=center, energy=900.0, color=logo.srgb("#dff3ff"), shape="RECTANGLE", size=0.4, size_y=4.0,
        )  # fmt: skip
    # The floor mirrors the back light and the low rims as white streaks from some angles;
    # they only exist to light the glass.
    for name in ("back", "rim-left", "rim-right"):
        logo.link_lights(rig[name], [floor], f"{name}-skips-floor", exclude=True)
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


def build(slide: float, *, frozen: bool = False) -> dict[str, bpy.types.Object]:
    """Build the model with the given tower slide and stage it, in clear crystal or frosted ice."""
    logo.TOWER_SLIDE = slide
    logo.HOLLOW = False
    logo.GLASS_BEVEL = (0.04, 6)
    logo.CUBE_BEAM = 0.11  # Slimmer beams open the frame enough to see the inner cube.
    logo.build_scene()
    scene = bpy.context.scene
    crystallize(scene, frozen=frozen)
    collection = logo.new_collection("crystal-set")
    build_tesseract(collection)
    leg_frames(collection)
    leg_glow(collection)
    rig = studio(collection)
    rig["core"] = bpy.data.objects["core"]
    camera = logo.add_camera("crystal-camera", collection)
    hero = bpy.data.objects["hero-camera"]
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = hero.data.ortho_scale
    camera.data.clip_end = 300.0
    camera.location = hero.location.copy()
    camera.rotation_euler = hero.rotation_euler.copy()
    scene.camera = rig["camera"] = camera
    return rig


# ---------------------------------------------------------------- effects


def ease(s: float) -> float:
    """Smoothstep easing on [0, 1]."""
    s = min(max(s, 0.0), 1.0)
    return s * s * (3 - 2 * s)


def lock_in(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """Glide from a low, wide, off-axis view into the logo's single viewpoint."""
    camera = rig["camera"]
    camera.data.type = "PERSP"
    camera.data.sensor_width = 36.0
    s = ease((frame - 1) / (FRAMES - LOCK_IN_HOLD - 1))
    azimuth = math.radians(45.0 + 80.0 * (1 - s))
    elevation = 0.17 + (logo.PHI - 0.17) * s
    camera.data.lens = 45.0 * (1 - s) + 400.0 * s  # A dolly zoom toward orthographic; the framing holds.
    view = Vector(
        (math.cos(azimuth) * math.cos(elevation), math.sin(azimuth) * math.cos(elevation), math.sin(elevation)),
    )
    camera.location = logo.HERO_TARGET + (1024.0 / logo.PX_PER_UNIT * camera.data.lens / 36.0) * view
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


def ignition(rig: dict[str, bpy.types.Object], frame: int) -> None:
    """A spark at the center, the inner cube traces on, struts reach the frame, light fills in, and the legs wake."""
    spark = ease((frame - 10) / 12)
    light = ease((frame - 50) / 40)
    legs = ease((frame - 72) / 30)
    bpy.data.materials["core-light"].node_tree.nodes["Emission"].inputs["Strength"].default_value = 400.0 * spark
    rig["core"].data.energy = 700.0 * light
    bpy.data.materials["lantern"].node_tree.nodes["Emission"].inputs["Strength"].default_value = 0.6 * light
    bpy.data.materials["leg-glow"].node_tree.nodes["glow-falloff"].inputs["To Min"].default_value = LEG_GLOW * legs
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


def render(effect: str, output_dir: Path, resolution: int, samples: int, *, frozen: bool = False) -> None:
    """Build and render one effect: a still PNG, or numbered frames in a folder."""
    rig = build(EFFECTS[effect], frozen=frozen)
    scene = bpy.context.scene
    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.cycles.samples = samples
    scene.cycles.adaptive_threshold = 0.02  # The denoiser cleans up the rest; frames stay affordable.
    scene.render.use_persistent_data = True  # Frames reuse the scene and resync only what changed.
    if effect == "still":
        scene.render.filepath = str(output_dir / "crystal.png")
        bpy.ops.render.render(write_still=True)
        return
    step = {"lock-in": lock_in, "ignition": ignition, "hyperspin": hyperspin}[effect]
    for frame in range(1, FRAMES + 1):
        step(rig, frame)
        scene.render.filepath = str(output_dir / effect / f"frame-{frame:04d}.png")
        bpy.ops.render.render(write_still=True)


def main() -> None:
    """Parse arguments after Blender's `--`, then render each requested effect."""
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", nargs="+", default=["still"], choices=list(EFFECTS))
    parser.add_argument("--resolution", type=int, default=1080)
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--blend", type=Path, help="Also save the scene of the last effect here.")
    parser.add_argument("--frozen", action="store_true", help="Frosted, cracked ice instead of clear crystal.")
    args = parser.parse_args(argv)
    for effect in args.render:
        render(effect, args.output_dir, args.resolution, args.samples, frozen=args.frozen)
    if args.blend:
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(args.blend), compress=True)


if __name__ == "__main__":
    main()
