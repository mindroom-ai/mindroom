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

function luminance([h, s, l]: [number, number, number]): number {
  const a = s * Math.min(l, 1 - l);
  const channel = (n: number) => {
    const k = (n + h / 30) % 12;
    const c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(8) + 0.0722 * channel(4);
}

function contrast(
  tokens: Map<string, [number, number, number]>,
  text: string,
  surface: string,
): number {
  const [light, dark] = [text, surface]
    .map((name) => {
      const value = tokens.get(name);
      if (!value) throw new Error(`missing --${name}`);
      return luminance(value);
    })
    .sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

const TEXT_PAIRS: [string, string][] = [
  ["foreground", "background"],
  ["foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "card"],
  ["muted-foreground", "secondary"],
  ["primary-foreground", "primary"],
  ["primary", "card"],
  ["accent-foreground", "accent"],
  ["destructive-foreground", "destructive"],
  ["destructive", "card"],
  ["rail-foreground", "rail"],
  ["rail-muted", "rail"],
  ["rail-accent", "rail"],
];

describe.each([":root", ".dark"])("%s theme tokens", (selector) => {
  const tokens = themeTokens(selector);

  it.each(TEXT_PAIRS)("%s on %s meets WCAG AA", (text, surface) => {
    expect(contrast(tokens, text, surface)).toBeGreaterThanOrEqual(4.5);
  });
});
