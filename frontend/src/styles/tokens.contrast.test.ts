import { readFileSync } from "node:fs";
import path from "node:path";

const css = readFileSync(path.resolve(__dirname, "../index.css"), "utf8");

// Reads the HSL triplets ("252 29% 97%") a theme block assigns to its custom properties.
function themeTokens(selector: string): Map<string, [number, number, number]> {
  const start = css.indexOf(`${selector} {`);
  const block = css.slice(start, css.indexOf("}", start));
  const tokens = new Map<string, [number, number, number]>();
  for (const [, name, h, s, l] of block.matchAll(
    /--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*;/g,
  )) {
    tokens.set(name, [Number(h), Number(s) / 100, Number(l) / 100]);
  }
  return tokens;
}

type Rgb = [number, number, number];

function rgb([h, s, l]: [number, number, number]): Rgb {
  const a = s * Math.min(l, 1 - l);
  const channel = (n: number) => {
    const k = (n + h / 30) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [channel(0), channel(8), channel(4)];
}

function luminance(color: Rgb): number {
  const [r, g, b] = color.map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

// A surface is a token, or a token tint at some opacity over another token
// (bg-destructive/10 on the page ground is ["destructive", 0.1, "background"]).
type Surface = string | [string, number, string];

function contrast(
  tokens: Map<string, [number, number, number]>,
  text: string,
  surface: Surface,
): number {
  const color = (name: string) => {
    const value = tokens.get(name);
    if (!value) throw new Error(`missing --${name}`);
    return rgb(value);
  };
  let surfaceColor: Rgb;
  if (typeof surface === "string") {
    surfaceColor = color(surface);
  } else {
    const [tint, alpha, base] = surface;
    const [top, bottom] = [color(tint), color(base)];
    surfaceColor = top.map(
      (c, i) => alpha * c + (1 - alpha) * bottom[i],
    ) as Rgb;
  }
  const [light, dark] = [luminance(color(text)), luminance(surfaceColor)].sort(
    (x, y) => y - x,
  );
  return (light + 0.05) / (dark + 0.05);
}

const TEXT_PAIRS: [string, Surface][] = [
  ["foreground", "background"],
  ["foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "card"],
  ["muted-foreground", "secondary"],
  ["muted-foreground", "active"],
  ["primary-foreground", "primary"],
  ["primary", "card"],
  ["primary", "background"],
  ["accent-foreground", "accent"],
  ["destructive-foreground", "destructive"],
  ["destructive", "card"],
  // Outlined destructive buttons on hover, and destructive badges, on every
  // surface they sit on, including selected and muted rows.
  ["destructive", ["destructive", 0.1, "background"]],
  ["destructive", ["destructive", 0.1, "card"]],
  ["destructive", ["destructive", 0.1, "muted"]],
  ["destructive", ["destructive", 0.1, "active"]],
  ["foreground", "active"],
];

describe.each([":root", ".dark"])("%s theme tokens", (selector) => {
  const tokens = themeTokens(selector);

  it.each(TEXT_PAIRS)("%s on %j meets WCAG AA", (text, surface) => {
    expect(contrast(tokens, text, surface)).toBeGreaterThanOrEqual(4.5);
  });
});
