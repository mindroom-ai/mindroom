# 3D MindRoom logo

![Hero render matching the SVG](hero.png)

The SVG logo is an orthographic drawing of a real object, so this directory rebuilds that object in Blender.
`build_scene.py` lifts the named corners from [`../artwork.py`](../artwork.py) back into 3D, saves `mindroom-logo.blend`, and renders it.
From the hero camera the model reproduces the SVG silhouette; from other angles its parts drift apart, because the M forms from that one viewpoint only.

| File | Purpose |
| --- | --- |
| `build_scene.py` | Builds the geometry, materials, lights, cameras, and render settings from scratch. |
| `mindroom-logo.blend` | Saved scene with live boolean and bevel modifiers, ready to edit or animate. |
| `hero.png` | Orthographic render on the SVG background and navy frame, 1024 × 1024. |
| `crystal.py` | Restyles the model as framed crystal or ice legs, glowing white, around a tesseract and renders its effects. |
| `crystal.blend` | Saved crystal scene for the still. |
| `crystal.png` | Clear crystal M on a dark mirror floor from the logo's viewpoint, 2160 × 2160. |
| `crystal-reveal.mp4` | Light fills the frozen M while the camera swings in, flashes as the M forms, and melts the ice; 1440 × 1440. |
| `crystal-lock-in.mp4` | The camera glides from a scattered view into the one viewpoint where the M forms, and it flashes. |
| `crystal-ignition.mp4` | A spark at the center; the inner cube traces on, its struts reach the frame, and the legs wake. |
| `crystal-hyperspin.mp4` | The tesseract turns once in 4D, the inner cube passing through the outer one; it loops. |

## Rebuild

From the repository root, with Blender 5.2 or newer on `PATH`:

```sh
blender --background --factory-startup --python assets/logo/blender/build_scene.py -- --render
```

The script always rebuilds and saves the `.blend` first; `--render` also renders the hero still afterwards.
Use `--resolution` and `--samples` for quick previews, and `--output-dir` or `--blend` to write elsewhere.
Rendering uses Cycles on the CPU; the still takes a few minutes on a 12-core machine.

## Crystal look and effects

![Crystal M around a glowing tesseract](crystal.png)

`crystal.py` imports the geometry from `build_scene.py` and restyles it after [`../social-preview.png`](../social-preview.png).

```sh
blender --background --factory-startup --python assets/logo/blender/crystal.py -- \
  --render still --resolution 1080 --samples 256 --blend assets/logo/blender/crystal.blend
blender --background --factory-startup --python assets/logo/blender/crystal.py -- \
  --render hyperspin ignition lock-in reveal --resolution 960 --samples 48 --output-dir /tmp/mindroom-crystal
```

Add `--frozen` to either command for frosted, cracked ice instead of clear crystal.

Each effect writes 120 numbered frames, four seconds at 30 fps, and the reveal writes 165; `ffmpeg` joins them into a video:

```sh
ffmpeg -framerate 30 -i /tmp/mindroom-crystal/reveal/frame-%04d.png \
  -c:v libx264 -pix_fmt yuv420p -crf 16 assets/logo/blender/crystal-reveal.mp4
```

On a 12-core CPU the clear still takes about 1.5 minutes at these settings, and `--frozen` takes about three times as long.
The committed renders are larger: the still is 2160 × 2160 at 1024 samples, the reveal 1440 × 1440 and the other effects 1080 × 1080 at 128 samples, rendered on a GPU together with the CPU.
The first run downloads the CC0 [Poly Haven](https://polyhaven.com/a/studio_small_09) studio HDRI into `~/.cache/mindroom-logo/`.

The look rests on a few choices:

- The towers and blocks are solid glass with rounded 0.04-unit edges (`HOLLOW = False`); thin hollow walls read as acrylic boxes.
- The glass is tinted at its surface rather than by thickness, so the tall towers stay azure, and faint internal scattering lets it glow from within like ice.
- The set is dark: a glossy navy floor, an HDRI for reflections that fades out toward the horizon, a high back light, rim strips, and a cool pool on the floor behind the letter for the glass to refract.
- Each tower and foot sits in a navy frame like the cube's, around a white light that fades from its center, so the legs glow from within.
- `--frozen` turns the crystal into ice: a paler tint, patchy frost, a hammered surface, and thin fracture planes of dense scattering along the edges of large Voronoi cells.
  Each of these fades with distance behind a thaw front, which the reveal sweeps outward from the cube to melt the ice.
- The cube is a tesseract: a slimmer navy frame (`CUBE_BEAM = 0.11`) around an inner cube of gold filaments, joined corner to corner by struts.
  Panes are left out because they mirror the azure towers over the gold, and the core light skips the frame so its beams stay dark.

The tesseract is computed rather than modeled: the 16 corners of a 4D cube are rotated in the x-w plane and projected from a 4D viewpoint at distance 4, which draws the far cell at 3/5 the size of the near one.
At angle 0 the near cell matches the navy frame and the far cell is the glowing inner cube.

- **Lock-in** starts low and off to the side, where the parts are visibly apart.
  A dolly zoom lengthens the lens toward orthographic while the camera swings into the logo's view, and the lights and HDRI turn with the camera.
  When the camera arrives and the M forms, a flash bursts from the center: the core floods the scene with light, haze around the letter shows its rays streaming out through the frame, and the exposure and lens streaks flare before fading within about half a second.
- **Ignition** lights a spark at the center, traces the inner cube's edges, grows the struts outward, fills in the light, and finally wakes the glow in the legs.
- **Hyperspin** turns the tesseract once in 4D; the gold inner cube grows through the frame while the outer cell shrinks inward, and the loop is seamless.
- **Reveal** builds to a climax during the lock-in glide, always starting in ice: light rises in the unlit legs from the floor, crosses into the cube as the outer cube traces on and the struts grow inward, and closes the inner cube just as the camera arrives; then a long flash swells, holds while its rays spread, and melts the ice into clear crystal from the cube outward as it settles.

## How the SVG becomes 3D

Every receding edge in the drawing moves 154 px across for each 96 px down, which fixes an orthographic camera looking down at `asin(96/154)`, about 38.6°.
With that projection, a 2D corner becomes a 3D point once one of its coordinates is known, for example that a roof corner lies on the top plane.
The cube's back-top corner at (512, 265) and front-bottom corner at (512, 643.5) set the scale; the cube is 9.5% taller than wide, as drawn.

The drawing's details turn out to be real geometry:

- The towers and lower blocks are hollow glass boxes.
  The thin strip beside each tower's front edge ("tower-gold" in `artwork.py`) is the 0.13-unit front wall seen through the side wall.
- A glass bridge joins each tower to the cube's back face at roof height, and the recessed "well" is an open skylight in that bridge.
- The central cube is a navy beam frame with amber panes; the lines inside it are the far beams seen through the glass.

The right wing mirrors the left across the vertical plane through the cube's front and back edges.

## A single-viewpoint M

Taken literally, the drawing places both towers a full unit behind the cube at its height, each hovering just above its lower block.
The strip the SVG paints as a block's cap is the block's top, seen through that gap.
The model keeps that layout, so the M exists only from the logo's angle and the parts drift apart elsewhere.
The lock-in and reveal effects turn that into a reveal: the camera swings into the one angle where the M forms.

## Look development

The hero camera is the SVG's orthographic view.
Its backdrop and the navy outline are flat polygons placed in screen space behind the model, using the SVG's colors and `structural-frame` path.
They are staging for that shot only: the outline is hidden from reflections and refraction, so the glass shows the backdrop behind it.

The SVG paints each face by the light that reaches it, and the materials follow that idea:

- A warm point light at the cube's center glows through the amber panes and lights the inner faces of the towers.
- An overhead key light and a reflection-only softbox give the roofs their light teal.
- A teal sky world with a navy horizon keeps the outer faces deep blue.
- The glass carries a faint diffuse layer on its outer surface, so faces take on the color of the light that reaches them.
- Glass lets shadow rays through with a tint, so the core lights the wings without caustic noise.

Cycles light linking keeps the key light and softbox off the amber panes, so the cube stays evenly gold.
The Khronos PBR Neutral view transform preserves brand colors while rolling off the bright core.
Its shadow toe would darken the flat backdrop, so the staging shaders add that offset back; the backdrop and outline display as the exact SVG colors.

## Glass modeling notes

Two Blender behaviors shaped the geometry code:

- Cycles tracks the volume a ray is inside by material.
  The outer skin and the cavity walls of a hollow block therefore share one material; with two materials, rays never leave the glass at the cavity and every block renders as solid.
  The logo look tells the cavity walls apart with a `cavity` face attribute instead.
- An exact boolean rebuilds the cut object's material list from both operands and merges duplicates, which can shift slot indices.
  Cutters carry no material, and the modifier uses index mode.
