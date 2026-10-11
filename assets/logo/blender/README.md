# 3D MindRoom logo

The SVG logo is an orthographic drawing of a real object, so this directory rebuilds that object in Blender and renders it as luminous crystal around a glowing tesseract, restyled after [`../social-preview.png`](../social-preview.png).
Renders are not committed; the commands below reproduce them.

| File | Purpose |
| --- | --- |
| `model.py` | Lifts the named corners from [`../artwork.py`](../artwork.py) back into 3D and builds the model with the materials it is given. |
| `crystal.py` | Dresses the model in crystal, stages it on a dark mirror floor, and renders a still or an effect. |

## Render

From the repository root, with Blender 5.2 or newer on `PATH`:

```sh
blender --background --factory-startup --python assets/logo/blender/crystal.py -- \
  --render still --resolution 2160 --samples 1024 --output-dir /tmp/mindroom-crystal
blender --background --factory-startup --python assets/logo/blender/crystal.py -- \
  --render wallpaper --resolution 5120 --height 2160 --samples 512 --output-dir /tmp/mindroom-crystal
blender --background --factory-startup --python assets/logo/blender/crystal.py -- \
  --render reveal lock-in ignition hyperspin --resolution 1080 --samples 128 --output-dir /tmp/mindroom-crystal
ffmpeg -framerate 30 -i /tmp/mindroom-crystal/reveal/frame-%04d.png \
  -c:v libx264 -pix_fmt yuv420p -crf 16 /tmp/mindroom-crystal/crystal-reveal.mp4
```

The still writes `crystal.png` and the wallpaper writes `crystal-wallpaper.png`, both as 16-bit PNGs so the dark gradients do not band.
The wallpaper frames the same view for a screen of `--resolution` by `--height` pixels, 16:9 unless `--height` is given, with the cube at its center and the M about half its shorter side; portrait phone sizes work too.
Each effect writes numbered frames into a folder of its own name, 120 frames (four seconds at 30 fps) or 165 for the reveal.
Add `--frozen` for frosted, cracked ice instead of clear crystal; the reveal always starts in ice and melts.
`--blend PATH` also saves the scene of the last effect for editing.
Rendering uses Cycles on the CPU: at 1080 × 1080 and 256 samples the clear still takes about 1.5 minutes on a 12-core machine, `--frozen` about three times as long, and time grows with pixels times samples.
The first run downloads the CC0 [Poly Haven](https://polyhaven.com/a/studio_small_09) studio HDRI into `~/.cache/mindroom-logo/`.

## Look

- The towers and blocks are solid glass with rounded 0.04-unit edges; thin hollow walls read as acrylic boxes.
- The glass is tinted at its surface rather than by thickness, so the tall towers stay azure, and faint internal scattering lets it glow from within like ice.
- The set is dark: a glossy navy floor, an HDRI for reflections that fades out toward the horizon, a high back light, rim strips, and a cool pool on the floor behind the letter for the glass to refract.
- Each tower and foot sits in a navy frame like the cube's, around a white light that fades from its center, so the legs glow from within.
- `--frozen` turns the crystal into ice: a paler tint, patchy frost, a hammered surface, and thin fracture planes of dense scattering along the edges of large Voronoi cells.
  Each of these fades with distance behind a thaw front, which the reveal sweeps outward from the cube to melt the ice.
- The cube is a tesseract: a slimmer navy frame (`CUBE_BEAM = 0.11`) around an inner cube of gold filaments, joined corner to corner by struts.
  It has no panes, because they would mirror the azure towers over the gold, and the core light skips the frame so its beams stay dark.

The tesseract is computed rather than modeled: the 16 corners of a 4D cube are rotated in the x-w plane and projected from a 4D viewpoint at distance 4, which draws the far cell at 3/5 the size of the near one.
At angle 0 the near cell matches the navy frame and the far cell is the glowing inner cube.

## Effects

- **Reveal** builds to a climax during the lock-in glide, always starting in ice: light rises in the unlit legs from the floor, crosses into the cube as the outer cube traces on and the struts grow inward, and closes the inner cube just as the camera arrives; then a long flash swells, holds while its rays spread, and melts the ice into clear crystal from the cube outward as it settles.
- **Lock-in** starts low and off to the side, where the parts are visibly apart.
  A dolly zoom lengthens the lens toward orthographic while the camera swings into the logo's view, and the lights and HDRI turn with the camera.
  When the camera arrives and the M forms, a flash bursts from the center: the core floods the scene with light, haze around the letter shows its rays streaming out through the frame, and the exposure and lens streaks flare before fading within about half a second.
- **Ignition** lights a spark at the center, traces the inner cube's edges, grows the struts outward, fills in the light, and finally wakes the glow in the legs.
- **Hyperspin** turns the tesseract once in 4D; the gold inner cube grows through the frame while the outer cell shrinks inward, and the loop is seamless.

## How the SVG becomes 3D

Every receding edge in the drawing moves 154 px across for each 96 px down, which fixes an orthographic camera looking down at `asin(96/154)`, about 38.6°.
With that projection, a 2D corner becomes a 3D point once one of its coordinates is known, for example that a roof corner lies on the top plane.
The cube's back-top corner at (512, 265) and front-bottom corner at (512, 643.5) set the scale; the cube is 9.5% taller than wide, as drawn.

The drawing's details turn out to be real geometry:

- A glass bridge joins each tower to the cube's back face at roof height, and the recessed "well" is an open skylight in that bridge.
- The central cube is a beam frame; the lines inside it are the far beams seen through the drawing's amber panes.
- The right wing mirrors the left across the vertical plane through the cube's front and back edges.

Taken literally, the drawing places both towers a full unit behind the cube at its height, each hovering just above its lower block.
The strip the SVG paints as a block's cap is the block's top, seen through that gap.
The model keeps that layout, so the M exists only from the logo's angle and the parts drift apart elsewhere; the lock-in and reveal turn that into their reveal.

An exact boolean rebuilds the cut object's material list from both operands and merges duplicates, which can shift slot indices, so cutters carry no material and the modifier uses index mode.
