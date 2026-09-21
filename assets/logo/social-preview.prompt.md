# Social artwork provenance

The social card was edited with OpenAI's `gpt-image-2.5-sunburst` through the Image API on September 21, 2026.
Both edits used `quality=high` and `size=auto`; the final edit also used `background=opaque`.
The input concept showed a cyan glass M on ivory, with a large wordmark and tagline to its left.
The first edit centered the logo and wordmark; the second refined the background and typography.
AI generation is not deterministic, so the committed PNG is the authoritative artwork.
No API access is needed to use it or regenerate the separate SVG app icons.

The final 1774 × 887 RGB result was resized without cropping to 1280 × 640 with Pillow 10.4.0's Lanczos filter and saved as an optimized PNG.
The export contains no credential, file-path, or prompt metadata.

## Centered layout prompt

```text
Use case: precise-object-edit
Asset type: professional GitHub organization social image, wide landscape 2:1 aspect ratio.
Input image 1 is the edit target and reference for the glass MindRoom M sculpture and warm ivory palette.
Primary request: redesign this card as a restrained centered brand lockup. Move the glass M logo to the horizontal center, with the single word "mindroom" centered directly beneath it. Remove the existing oversized left title and remove the tagline completely.
Preserve the recognizable architectural M silhouette, cyan and teal transparent glass, deep navy edges, and soft amber light glowing inside the center. Preserve the elegant warm ivory studio background. Refine the glass so it looks polished and luminous, with smooth internal glow and crisp rims. Keep caustics subtle and close to the base, not dramatic streaks.
Composition: wide 2:1 canvas; logo and wordmark form one vertically stacked unit at the center of the image, with generous balanced negative space left and right. Logo occupies about half the canvas height. Wordmark sits comfortably below, with a clear gap and no overlap, about one tenth of the canvas height. Center the whole unit optically. No app-icon rounded-square tile behind the M.
Text (verbatim, all lowercase): "mindroom".
Typography: a beautifully drawn contemporary humanist sans serif, medium weight, dark ink navy, carefully spaced, subtly distinctive but simple and professional. A refined brand wordmark, not futuristic, not monospaced, not a heavy tech font. Ordinary readable letterforms, no gimmicks or exaggerated tracking.
Constraints: only the glass M and the single word mindroom. No slogan, URL, small print, labels, borders, secondary icons, circuitry, room diagrams, sparkles, particles, or additional decoration. Keep the original brand mark recognizable. Premium, warm, calm, understated.
```

## Final refinement prompt

```text
Use case: precise-object-edit
Edit the supplied centered MindRoom social card. Keep the centered M sculpture, its exact recognizable silhouette, translucent cyan glass and warm amber inner glow, and the single lowercase word "mindroom" underneath. Keep the same 2:1 wide composition and placement.
Correct the background: fill the ENTIRE canvas with a fully opaque, smooth warm ivory studio background, edge to edge, including all empty margins and behind the text. No transparency anywhere, no black background, no checkerboard, no spotlight halo cutout or vignette edge. Let very subtle soft shadow and a small glass caustic ground the sculpture naturally on the ivory surface.
Refine the wordmark: retain "mindroom" spelled exactly, all lowercase, dark navy. Use a professional contemporary humanist sans serif with MEDIUM stroke weight, lighter and more elegant than the supplied heavy bold text. Refined kerning, ordinary readable letterforms, warm and confident, not futuristic, geometric-tech or monospaced. Keep it centered and comfortably separated below the logo. Keep generous clean ivory negative space around the central logo and text.
Only the M logo and word mindroom. No tagline, URL, decorative objects, particles or extra elements. Final output must be completely opaque.
```
