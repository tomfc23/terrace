import * as THREE from "three";

export const HEIGHT_SCALE = 4;
export const BASE_THICKNESS_MM = 3;
export const PRINT_LENGTH_MM = 180;

// Shape-preserving cubic interpolation rounds slopes without inventing negative
// activity or extra peaks between the original histogram samples.
function interpolate(values, position) {
  const p = Math.max(0, Math.min(values.length - 1, position));
  const i = Math.min(Math.floor(p), values.length - 2);
  const t = p - i;
  const a = values[Math.max(0, i - 1)], b = values[i];
  const c = values[i + 1], d = values[Math.min(values.length - 1, i + 2)];
  const slope = (u, v) => u * v <= 0 ? 0 : 2 * u * v / (u + v);
  const m0 = slope(b - a, c - b), m1 = slope(c - b, d - c);
  return (2 * t ** 3 - 3 * t ** 2 + 1) * b
    + (t ** 3 - 2 * t ** 2 + t) * m0
    + (-2 * t ** 3 + 3 * t ** 2) * c + (t ** 3 - t ** 2) * m1;
}

export function createRelief(geo, roundedEdges = true, printLengthMM = PRINT_LENGTH_MM, baseThicknessMM = BASE_THICKNESS_MM) {
  if (![printLengthMM, baseThicknessMM].every(value => Number.isFinite(value) && value > 0) || baseThicknessMM > 50) {
    throw new RangeError("Print length must be positive and base thickness must be greater than zero and at most 50 mm.");
  }
  const [length, width] = geo.pitch;
  const [nx, ny] = geo.shape;
  const positions = [], indices = [];
  const taperWidth = Math.min(length, width) * 0.1;
  const overflow = roundedEdges ? taperWidth : 0;
  const printScale = printLengthMM / (length + 2 * overflow);
  const baseHeight = baseThicknessMM / printScale;
  // Keep the original field samples identical in both modes; add the slope outside them.
  const samples = (size, segments) => {
    const extra = Math.ceil(overflow / (size / segments));
    return [
      ...Array.from({ length: extra }, (_, i) => -overflow + i / extra * overflow),
      ...Array.from({ length: segments + 1 }, (_, i) => i / segments * size),
      ...Array.from({ length: extra }, (_, i) => size + (i + 1) / extra * overflow),
    ];
  };
  const xs = samples(length, Math.max(160, nx * 4));
  const zs = samples(width, Math.max(104, ny * 4));
  const sx = xs.length - 1, sy = zs.length - 1;
  const taper = distance => {
    const t = Math.min(1, Math.max(0, distance / taperWidth));
    return t * t * (3 - 2 * t);
  };
  const columns = Array.from({ length: sx + 1 }, (_, i) =>
    Array.from({ length: ny }, (_, j) => interpolate(
      geo.z.map(row => row[j]), xs[i] / length * nx - 0.5,
    )),
  );
  for (let i = 0; i <= sx; i++) {
    for (let j = 0; j <= sy; j++) {
      const x = xs[i], z = zs[j];
      // Only the overflow slopes down; activity inside the field is untouched.
      const edgeScale = roundedEdges
        ? taper(x + overflow) * taper(length + overflow - x)
          * taper(z + overflow) * taper(width + overflow - z) : 1;
      positions.push(x,
        baseHeight + HEIGHT_SCALE * interpolate(columns[i], z / width * ny - 0.5) * edgeScale,
        z);
    }
  }
  const topVertexCount = positions.length / 3;
  const at = (i, j) => i * (sy + 1) + j;
  for (let i = 0; i < sx; i++) {
    for (let j = 0; j < sy; j++) {
      const a = at(i, j), b = at(i + 1, j), c = at(i + 1, j + 1), d = at(i, j + 1);
      indices.push(a, d, b, d, c, b);
    }
  }
  // Duplicate the rim to keep the base walls crisp while the terrain shades smoothly.
  const perimeter = [];
  for (let i = 0; i < sx; i++) perimeter.push(at(i, 0));
  for (let j = 0; j < sy; j++) perimeter.push(at(sx, j));
  for (let i = sx; i > 0; i--) perimeter.push(at(i, sy));
  for (let j = sy; j > 0; j--) perimeter.push(at(0, j));
  const rim = positions.length / 3;
  for (const vertex of perimeter) {
    const [x, y, z] = positions.slice(vertex * 3, vertex * 3 + 3);
    positions.push(x, y, z, x, 0, z);
  }
  const centre = positions.length / 3;
  positions.push(length / 2, 0, width / 2);
  for (let k = 0; k < perimeter.length; k++) {
    const a = rim + k * 2, b = rim + ((k + 1) % perimeter.length) * 2;
    indices.push(a, b, a + 1, b, b + 1, a + 1, centre, a + 1, b + 1);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();

  return { geometry, baseHeight, topVertexCount, printScale };
}
