// Ported from MindRoom Chat (mindroom-ai/mindroom-chat src/app/components/glass/liquid/displacement.test.ts).
import { createDisplacementMap } from './displacement';

describe('liquid glass displacement', () => {
  // Decode the SVG contract, rather than repeating the map's geometry.
  const shiftAt = (
    map: NonNullable<ReturnType<typeof createDisplacementMap>>,
    x: number,
    y: number
  ) => {
    const offset = (y * map.width + x) * 4;
    return [
      map.scale * (map.data[offset] / 255 - 0.5),
      map.scale * (map.data[offset + 1] / 255 - 0.5),
    ];
  };

  it('bends all four rims inward while keeping the middle clear', () => {
    const map = createDisplacementMap(200, 100, 20)!;
    const left = shiftAt(map, 3, 50);
    const right = shiftAt(map, 196, 50);
    const top = shiftAt(map, 100, 3);
    const bottom = shiftAt(map, 100, 96);
    expect(left[0]).toBeGreaterThan(3);
    expect(right[0]).toBeLessThan(-3);
    expect(top[1]).toBeGreaterThan(3);
    expect(bottom[1]).toBeLessThan(-3);
    expect(Math.abs(left[1])).toBeLessThan(0.1);
    expect(Math.abs(top[0])).toBeLessThan(0.1);
    expect(left[0] + right[0]).toBeCloseTo(0, 1);
    expect(top[1] + bottom[1]).toBeCloseTo(0, 1);
    expect(shiftAt(map, 100, 50).every((value) => Math.abs(value) < 0.1)).toBe(true);
  });

  it('curves the lens around rounded corners without warping clipped outside pixels', () => {
    const map = createDisplacementMap(200, 100, 20)!;
    const corner = shiftAt(map, 8, 8);
    expect(corner[0]).toBeGreaterThan(2);
    expect(corner[1]).toBeCloseTo(corner[0], 1);
    expect(shiftAt(map, 0, 0).every((value) => Math.abs(value) < 0.1)).toBe(true);
  });

  it('bounds raster work for large surfaces while preserving their aspect ratio', () => {
    const map = createDisplacementMap(4000, 2000, 32)!;
    expect(map.width * map.height).toBeLessThanOrEqual(262144);
    expect(map.width / map.height).toBeCloseTo(2, 1);
    expect(map.data.length).toBe(map.width * map.height * 4);
    expect(map.data.every((value, index) => index % 4 !== 3 || value === 255)).toBe(true);
  });

  it('clamps oversized radii into a capsule with finite, symmetric displacement', () => {
    const map = createDisplacementMap(44, 44, 9999)!;
    expect(shiftAt(map, 2, 22)[0]).toBeGreaterThan(2);
    expect(shiftAt(map, 41, 22)[0]).toBeLessThan(-2);
    expect(Number.isFinite(map.scale)).toBe(true);
  });

  it.each([
    [0, 100],
    [100, 0],
    [-1, 100],
    [NaN, 100],
    [100, Infinity],
  ])('skips unavailable dimensions %s × %s', (width, height) => {
    expect(createDisplacementMap(width, height, 12)).toBeNull();
  });
});
