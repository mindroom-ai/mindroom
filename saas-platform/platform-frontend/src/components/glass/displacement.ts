// Liquid glass from MindRoom Chat (mindroom-ai/mindroom-chat src/app/components/glass/liquid/displacement.ts), kept identical so both surfaces look the same.
export type DisplacementMap = {
  width: number;
  height: number;
  data: Uint8ClampedArray;
  scale: number;
};

export const createDisplacementMap = (
  width: number,
  height: number,
  radius: number
): DisplacementMap | null => {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;

  const ratio = Math.min(1, Math.sqrt(262144 / (width * height)), 2048 / Math.max(width, height));
  const rasterWidth = Math.max(1, Math.floor(width * ratio));
  const rasterHeight = Math.max(1, Math.floor(height * ratio));
  const data = new Uint8ClampedArray(rasterWidth * rasterHeight * 4);
  const corner = Math.min(Math.max(Number.isFinite(radius) ? radius : 0, 0), width / 2, height / 2);
  const bezel = Math.min(18, width / 4, height / 4, Math.max(6, corner));
  const thickness = bezel * 1.5;

  // A convex squircle cross-section has a steep outer rim and a flat center.
  // Snell's law (air -> glass, index 1.5) gives the inward sample displacement.
  const profile = new Float32Array(128);
  let maximum = 0;
  for (let index = 0; index < profile.length; index += 1) {
    const inward = Math.max(0.001, index / (profile.length - 1));
    const remaining = 1 - inward;
    const height = (1 - remaining ** 4) ** 0.25;
    const slope = remaining ** 3 / Math.max(height ** 3, 0.001);
    const incident = Math.atan(slope);
    const refracted = Math.asin(Math.sin(incident) / 1.5);
    profile[index] = Math.tan(incident - refracted) * thickness * (height + 0.15);
    maximum = Math.max(maximum, profile[index]);
  }

  // SVG samples at position + scale * (channel - 0.5), hence twice the
  // largest displacement is needed to use the complete 0..255 channel range.
  const scale = maximum * 2;
  for (let y = 0; y < rasterHeight; y += 1) {
    for (let x = 0; x < rasterWidth; x += 1) {
      const px = ((x + 0.5) / rasterWidth - 0.5) * width;
      const py = ((y + 0.5) / rasterHeight - 0.5) * height;
      const qx = Math.abs(px) - (width / 2 - corner);
      const qy = Math.abs(py) - (height / 2 - corner);
      const outerX = Math.max(qx, 0);
      const outerY = Math.max(qy, 0);
      const outerLength = Math.hypot(outerX, outerY);
      const distance = corner - outerLength - Math.min(Math.max(qx, qy), 0);
      let dx = 0;
      let dy = 0;
      if (distance >= 0 && distance < bezel && maximum > 0) {
        const magnitude = profile[Math.min(127, Math.round((distance / bezel) * 127))];
        const normalX = outerLength > 0 ? outerX / outerLength : Number(qx > qy);
        const normalY = outerLength > 0 ? outerY / outerLength : Number(qy >= qx);
        dx = -Math.sign(px) * normalX * magnitude;
        dy = -Math.sign(py) * normalY * magnitude;
      }
      const offset = (y * rasterWidth + x) * 4;
      data[offset] = Math.round(255 * (0.5 + dx / scale));
      data[offset + 1] = Math.round(255 * (0.5 + dy / scale));
      data[offset + 2] = 128;
      data[offset + 3] = 255;
    }
  }
  return { width: rasterWidth, height: rasterHeight, data, scale };
};
