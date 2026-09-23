# ruff: noqa: INP001 -- Standalone logo script modules, outside the application package.
"""Give the cube a quiet heartbeat and send gold currents through both legs.

Only lightweight overlay layers animate. The detailed sampled surface paints
stay static, and reduced-motion viewers see the original artwork.
"""

from copy import deepcopy

from artwork import SVG, group
from lxml import etree

# One shared clock keeps the cube's breathing and the traveling signals in
# phase. Change the cycle here to retime the entire animation.
MOTION_CSS = """
  :root { --mindroom-cycle: 6.4s; }
  .mindroom-core-light, .mindroom-core-shade, .mindroom-signal {
    opacity: 0;
  }
  @media (prefers-reduced-motion: no-preference) {
    .mindroom-core-light {
      animation: mindroom-breathe var(--mindroom-cycle) ease-in-out infinite;
    }
    .mindroom-core-shade {
      animation: mindroom-rest var(--mindroom-cycle) ease-in-out infinite;
    }
    .mindroom-signal {
      stroke-dasharray: 100;
      stroke-dashoffset: 100;
      animation: mindroom-current var(--mindroom-cycle) linear infinite;
    }
    .mindroom-signal-echo { animation-delay: 0.38s; }
  }
  @keyframes mindroom-breathe {
    0%, 100% { opacity: 0.02; }
    14% { opacity: 0.08; }
    28% { opacity: 0.48; }
    45% { opacity: 0.16; }
    72% { opacity: 0; }
  }
  @keyframes mindroom-rest {
    0%, 100% { opacity: 0.08; }
    14% { opacity: 0.05; }
    28%, 36% { opacity: 0; }
    66% { opacity: 0.1; }
  }
  @keyframes mindroom-current {
    0%, 17% { opacity: 0; stroke-dashoffset: 100; }
    21% { opacity: 0.6; stroke-dashoffset: 88; }
    32% { opacity: 1; stroke-dashoffset: 0; }
    37% { opacity: 0.8; stroke-dashoffset: 0; }
    49% { opacity: 0; stroke-dashoffset: -100; }
    100% { opacity: 0; stroke-dashoffset: -100; }
  }
"""

# Curves recall the small S-shaped filaments in the original PNG. They run
# outward from behind the central cube toward the inside vertical strut.
SIGNALS = (
    "M 367,477 C 354,464 351,451 334,446 C 314,441 307,440 308.5,412",
    "M 363,500 C 350,484 339,490 330,477 C 322,466 326,458 316,451",
)


def radial_paint(defs: etree._Element, name: str, color: str, stops: tuple[tuple[float, float], ...]) -> str:
    """Keep the changing illumination centered on the cube's existing light."""
    gradient = etree.SubElement(
        defs,
        SVG + "radialGradient",
        id=name,
        gradientUnits="userSpaceOnUse",
        cx="512",
        cy="448",
        r="205",
    )
    for offset, opacity in stops:
        etree.SubElement(
            gradient,
            SVG + "stop",
            offset=str(offset),
            attrib={"stop-color": color, "stop-opacity": str(opacity)},
        )
    return f"url(#{name})"


def add_currents(root: etree._Element, defs: etree._Element) -> None:
    """Paint filaments inside the glass, behind the opaque central cube."""
    blur = etree.SubElement(
        defs,
        SVG + "filter",
        id="mindroom-current-glow",
        x="-40%",
        y="-40%",
        width="180%",
        height="180%",
    )
    etree.SubElement(blur, SVG + "feGaussianBlur", stdDeviation="2")
    for side in ("left", "right"):
        room = root.find(f".//{SVG}g[@id='{side}-rooms']")
        glass = root.find(f".//{SVG}path[@id='inner-glass-{side}-rooms']")
        clip_id = f"mindroom-current-glass-{side}"
        clip = etree.SubElement(defs, SVG + "clipPath", id=clip_id)
        etree.SubElement(clip, SVG + "path", d=glass.get("d"))
        currents = group(room, f"mindroom-currents-{side}", "Gold filaments appear as the cube releases its pulse.")
        currents.set("clip-path", f"url(#{clip_id})")
        for index, curve in enumerate(SIGNALS):
            classes = "mindroom-signal" + (" mindroom-signal-echo" if index else "")
            filament = etree.SubElement(currents, SVG + "g", opacity="0.45" if index else "1")
            for width, color, opacity in ((7, "#ffd77e", "0.35"), (1.7, "#fff7c8", "1")):
                # Opacity on the wrapper scales the halo independently of the
                # animation, which controls the path's opacity and dash offset.
                paint = etree.SubElement(filament, SVG + "g", opacity=opacity)
                path = etree.SubElement(
                    paint,
                    SVG + "path",
                    d=curve,
                    pathLength="100",
                    fill="none",
                    stroke=color,
                    attrib={"class": classes, "stroke-width": str(width), "stroke-linecap": "round"},
                )
                if width > 2:
                    path.set("filter", "url(#mindroom-current-glow)")


def animated_document(source: etree._Element) -> etree._Element:
    """Return a self-contained animated copy, preserving the static source."""
    root = deepcopy(source)
    root.find(SVG + "title").text = "MindRoom illuminated glass M with breathing light and gold currents"
    root.find(
        SVG + "desc",
    ).text += " The cube gently pulses and sends curved gold signals through both legs. Reduced-motion preferences show the static artwork."
    style = etree.Element(SVG + "style")
    style.text = MOTION_CSS
    root.insert(2, style)
    defs = root.find(SVG + "defs")
    defs.append(etree.Comment(" Animation paints: small overlays leave the sampled surface lighting unchanged. "))
    light = radial_paint(defs, "mindroom-core-warmth", "#fff2aa", ((0, 0.95), (0.45, 0.6), (1, 0)))
    shade = radial_paint(defs, "mindroom-core-rest", "#17374c", ((0, 0.95), (0.65, 0.55), (1, 0)))
    add_currents(root, defs)
    cube = root.find(f".//{SVG}g[@id='central-cube']")
    glow = group(cube, "mindroom-core-energy", "A quiet rise and fall in warmth; the logo geometry stays still.")
    silhouette = "M 512,265 666,361 666,546 512,643 358,546 358,361 Z"
    etree.SubElement(glow, SVG + "path", d=silhouette, fill=shade, attrib={"class": "mindroom-core-shade"})
    etree.SubElement(glow, SVG + "path", d=silhouette, fill=light, attrib={"class": "mindroom-core-light"})
    return root
