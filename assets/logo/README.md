# MindRoom logo source

![Generated MindRoom logo](preview.png)

This directory contains the editable source and generated artwork for the logo refinement in [#2044](https://github.com/mindroom-ai/mindroom/issues/2044).
The SVG is generated locally with Python.
No image-generation service or API key is needed to rebuild it.

## Edit and regenerate

From the repository root:

```sh
uv run assets/logo/generate.py
```

The PNG files use Git LFS.
If your checkout contains pointer files, fetch the images with `git lfs pull --include='assets/logo/*.png'` first.

The script declares its own pinned rendering dependencies; they are separate from the application dependencies.
It writes the static `logo.svg`, `logo-transparent.svg`, and `preview.png`, plus `logo-animated.svg` and `logo-animated-transparent.svg`.

| File | Purpose |
| --- | --- |
| `artwork.py` | Named vertices, face polygons, highlight widths, layer order, and mirrored parts. Edit the shape here. |
| `geometry.py` | Intersections and shared miter geometry for two-, three-, and four-way junctions. |
| `shading.py` | Samples illumination from the reference and encodes it as SVG gradients and masks. |
| `generate.py` | Builds, shades, formats, and exports the artwork. |
| `animation.py` | Cube pulse, curved electrical filaments, and their shared timing. |
| `preview.html` | Browser preview with a pause/play control. |
| `reference.png` | Cleaned raster design used as the lighting reference. |
| `test_geometry.py` | Regression checks for closed junctions and angled terminal cuts. |

Faces and highlights refer to the same named corner coordinates.
At a junction, adjacent offset edges intersect to form a shared miter, and each filled edge polygon reaches the corner center.
An edge ending against a beam is cut along that beam's angle.
If an edit leaves too little space between neighboring miters, generation fails with the offending edge's name.
The central vertical is clipped to the pointed frame silhouette.
Clipped backing paint prevents antialias hairlines between touching facets without changing their outside contour.

The two room wings and cube side panels share authored geometry and are mirrored during export.
Mirrored instances receive separate paint definitions to keep masked shading consistent across SVG renderers.

## How the reference is used

The PNG supplies colors and illumination, while Python defines the geometry.
Each face is rasterized into an ownership mask before its interior colors are sampled.
Those samples become a grid of horizontal linear gradients blended vertically with SVG masks.
Thin highlights use gradients sampled along each edge, with shared colors at their endpoints.

The resulting SVG contains native vector shapes, gradients, patterns, and masks; it embeds no bitmap and loads no external resources.
The detailed lighting makes the generated files larger than a flat-color logo.
Generated XML is indented, with named, commented geometry layers first and sampled paint definitions afterward.
Edit the Python source and regenerate, because rebuilding replaces direct changes to the SVG.

## Animated version

The cube gently warms and dims over a 6.4-second cycle.
As the light rises, gold S-shaped currents unfurl toward the inner struts, followed by a fainter echo, then fade into the glass.
The filaments recall the curved traces in the original PNG.
Room clipping and layer order keep them inside the glass and behind the central cube.

The SVG carries its own CSS animation and needs no JavaScript to play.
Only small overlay layers animate; the detailed surface paints stay static.
The system's [reduced-motion preference](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion) shows the static artwork instead.
Change `--mindroom-cycle`, the keyframes, or `SIGNALS` in `animation.py` to tune the timing, light intensity, or curves.

To inspect the animation with a pause control, serve this directory:

```sh
uv run --no-project python -m http.server 8768 --directory assets/logo
```

Then open [the motion preview](http://localhost:8768/preview.html).
The animated SVGs can also be embedded as ordinary SVG images.

## Checks

```sh
# Verify that the committed SVGs and PNG match their source.
uv run assets/logo/generate.py --check

# Exercise the geometry without installing application dependencies.
uv run --isolated --no-project --with pytest==8.4.2 pytest \
  -c /dev/null -p no:cacheprovider assets/logo/test_geometry.py -q
```

The geometry tests cover shared two-, three-, and four-way corners, unequal widths, straight continuations, angled cuts, and rejection of crossed miters.
The logo workflow runs these tests and regenerates the committed outputs in check mode.
For visual review, rasterize the complete SVG at the desired resolution before cropping individual junctions; keep the original `viewBox` so pattern coordinates remain unchanged.
Inspect enlarged junctions as well as the full logo, because a whole-image pixel error can hide local edge defects.

These are source artwork and review exports; application asset adoption can be reviewed separately.
