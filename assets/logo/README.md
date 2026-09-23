# MindRoom logo source

![Generated MindRoom logo](logo.svg)

This directory contains the editable source and generated artwork for the logo refinement in [#2044](https://github.com/mindroom-ai/mindroom/issues/2044).
The SVG is generated locally with Python.
No image-generation service or API key is needed to rebuild it.

## Edit and regenerate

From the repository root:

```sh
uv run assets/logo/generate.py
```

The PNG files use Git LFS.
If your checkout contains pointer files, fetch the images with `git lfs pull` first.

The script declares its own pinned rendering dependencies; they are separate from the application dependencies.
It writes the static `logo.svg`, `logo-transparent.svg`, and `preview.png`, plus `logo-animated.svg` and `logo-animated-transparent.svg`.
Each SVG also has a losslessly compressed `.svgz` copy.
It also writes `logo-transparent.png` as a raster preview of the complete M with a transparent background.
The `logo-mark.svg` and `logo-mark-animated.svg` exports tightly frame the static and animated M for small icons and the README.

| File | Purpose |
| --- | --- |
| `artwork.py` | Named vertices, face polygons, highlight widths, layer order, and mirrored parts. Edit the shape here. |
| `geometry.py` | Intersections and shared miter geometry for two-, three-, and four-way junctions. |
| `shading.py` | Samples illumination from the reference and encodes it as SVG gradients and masks. |
| `generate.py` | Builds, shades, formats, and exports the artwork. |
| `animation.py` | Cube pulse, curved electrical filaments, and their shared timing. |
| `optimize.py` | Lossless sharing of identical gradients and stops, unused-paint removal, and compact XML. |
| `publication.py` | Frames the unchanged M and derives the app, documentation, favicon, desktop, and Matrix avatar exports. |
| `app_icons.py` | Composes dark-glass and light-porcelain app icon treatments around the shared vector M. |
| `app-glass-material.svg` | Editable refraction filter, glowing panes, and glass tile lighting for the dark app icon. |
| `social-preview.png` | AI-rendered ivory social card with a centered glass M and lowercase wordmark. |
| `social-preview.prompt.md` | Sunburst model and prompts used for the social artwork. |
| `menu-bar.svg` | Optional monochrome outline variant of the M. |
| `preview.html` | Browser preview with a pause/play control. |
| `reference.png` | Cleaned raster design used as the lighting reference. |
| `test_geometry.py` | Regression checks for closed junctions and angled terminal cuts. |
| `test_exports.py` | File-size budgets and exact SVGZ decompression checks. |
| `test_optimize.py` | Preservation of repeated stops through shared gradient templates. |
| `test_publication.py` | Complete M coverage, exact transparent PNG pixels, and unchanged artwork after reframing. |

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
Each generated paint definition occupies one line to reduce formatting overhead.
Edit the Python source and regenerate, because rebuilding replaces direct changes to the SVG.

## Smaller files without quality loss

The exporter shares exactly identical gradients and complete stop sequences, and discards unused paint definitions.
Every gradient stop is retained, including repeated colors, because removing redundant stops can change browser rasterization slightly.
It shortens generated IDs and removes redundant trailing decimal zeros without rounding coordinates or resampling colors.
Instance-specific masks and patterns remain separate for consistent rendering.
Before exporting each SVG, the generator compares all RGBA pixels of the original and optimized documents at 1024 pixels and fails if any differ.

The current background export is about 1.47 MB as plain SVG and 135 KB as SVGZ, compared with 2.45 MB before cleanup.
SVGZ is gzip-compressed SVG; decompressing it produces the exact companion SVG bytes.
The compressed figure is distinct from the plain XML file size.
Gzip timestamps are fixed, and check mode compares decompressed content so platform compression differences do not cause false failures.

For web use, ordinary `.svg` files can be served with gzip compression.
When serving `.svgz` directly, configure these [HTTP response headers](https://developer.mozilla.org/en-US/docs/Web/SVG/Tutorials/SVG_from_scratch/Getting_started):

```http
Content-Type: image/svg+xml
Content-Encoding: gzip
```

For GitHub README images, link to the ordinary `.svg` file.
A browser check on 2026-09-13 confirmed that GitHub gzip-compresses it automatically; the static background SVG transferred at about 140 KB and decoded to the exact exported bytes.
GitHub's raw `.svgz` response was compressed a second time and failed to display as an image.

## Transparent artwork and application assets

The transparent variants remove only the canvas background; the complete M, its navy frame, glass, and illumination remain opaque.
The transparent PNG matches the SVG render, and every opaque pixel matches the background preview.
The compact `logo-mark.svg` and `logo-mark-animated.svg` change only the viewport, keeping the original vector geometry and paint definitions intact.
Both use the same 720-pixel square viewport with a small border, so the M fills more of its displayed area without jumping when the motion preference changes.
Their regression tests check that the crop removes no painted pixels and that restoring the original viewport reproduces the exact RGBA image.

Regeneration also updates the dashboard and documentation SVGs, portal branding, PNG fallbacks, both web favicons, the macOS app icon source, and the bundled Matrix root-space avatar.
The macOS appearance variants use `app-icon-light.svg` and `app-icon-dark.svg`, rendered to `macos/MindRoom/Resources/MindRoom.icon/Assets/` as 1024-pixel PNGs.
These full-bleed images receive their final system mask from Icon Composer, which supplies appearance variants on macOS 26 or newer and a static light icon on older systems.
The material treatments were inspired by AI-generated concepts; their geometry, backgrounds, edge lighting, and shadows are rendered deterministically from SVG.
The dark icon takes inspiration from [Kube's liquid-glass article](https://kube.io/blog/liquid-glass-css-svg/): a blurred pane silhouette approximates a rounded surface, whose horizontal and vertical derivatives drive an SVG displacement filter.
Directional specular highlights and broad cyan glows give the panes depth, while an inset illuminated rim defines the dark glass tile.
This is a static approximation baked into the exported PNG, with no browser backdrop filter, embedded bitmap, or runtime effect.
The native macOS app bundles the mark SVG and its matching PNG; AppKit uses the PNG to preserve the SVG's masked shading.
Its menu bar icon uses dedicated 20- and 40-pixel renders of the full-color mark, preserving the original blue-and-gold shading without AppKit template tinting.
The portal's public logo aliases resolve within its own public directory so container builds retain them.
Application assets use the static version except for the connections page, which imports the existing `assets/logo/logo-mark-animated.svgz` directly.
The frontend build bundles that compressed asset, and the backend and Vite servers send it with SVG and gzip response headers.
The generated root-space avatar is a default asset; existing uploaded or custom Matrix avatars follow the existing avatar management behavior.

## GitHub social images

These PNGs are ready to upload:

| File | Size | Use |
| --- | --- | --- |
| [social-preview.png](social-preview.png) | 1280 × 640 | Repository Settings → Social preview → Upload an image. |
| [Dark app icon](../../macos/MindRoom/Resources/MindRoom.icon/Assets/dark.png) | 1024 × 1024 | Organization profile picture, reusing the dark glass M. |

Both PNGs have opaque backgrounds and stay below GitHub's 1 MB social preview limit.
The social card follows [GitHub's recommended dimensions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).
The social card is AI-rendered artwork created with `gpt-image-2.5-sunburst`; its model, prompts, and export details are recorded in [social-preview.prompt.md](social-preview.prompt.md).
It is a committed raster asset, and `generate.py` leaves it unchanged.
The app icons remain editable SVG, and the organization avatar reuses the macOS dark PNG without a duplicate export.

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

# Exercise geometry and export budgets without application dependencies.
uv run --isolated --no-project --with pytest==8.4.2 --with lxml==5.4.0 \
  --with numpy==2.4.4 --with pillow==10.4.0 --with resvg-py==0.5.0 --with scipy==1.17.1 pytest \
  -c /dev/null -p no:cacheprovider assets/logo/ -q
```

The geometry tests cover shared two-, three-, and four-way corners, unequal widths, straight continuations, angled cuts, and rejection of crossed miters.
Export tests enforce size budgets and byte-identical SVGZ decompression.
The optimization regression protects repeated gradient stops that affect browser rasterization.
The logo workflow runs these tests and regenerates the committed outputs in check mode.
For visual review, rasterize the complete SVG at the desired resolution before cropping individual junctions; keep the original `viewBox` so pattern coordinates remain unchanged.
Inspect enlarged junctions as well as the full logo, because a whole-image pixel error can hide local edge defects.

The repository README selects the tightly framed `logo-mark-animated.svg` when motion is allowed, with `logo-mark.svg` as its reduced-motion and compatibility fallback.
The connections header uses the animated framed transparent mark, whose embedded CSS honors reduced-motion preferences; other application headers use the static mark.
