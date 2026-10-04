// Liquid glass from MindRoom Chat (mindroom-ai/mindroom-chat src/app/components/glass/liquid/liquidGlass.ts), kept identical so both surfaces look the same.
import { createDisplacementMap } from './displacement';

const SVG_NS = 'http://www.w3.org/2000/svg';
const mapCache = new Map<string, { url: string; scale: number }>();
const roots = new WeakMap<Document, SVGSVGElement>();
let nextId = 0;

const svgElement = <K extends keyof SVGElementTagNameMap>(
  document: Document,
  tag: K,
  attributes: Record<string, string>
): SVGElementTagNameMap[K] => {
  const element = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
  return element;
};

const getMap = (document: Document, width: number, height: number, radius: number) => {
  const key = `${width}:${height}:${radius}`;
  const cached = mapCache.get(key);
  if (cached) {
    mapCache.delete(key);
    mapCache.set(key, cached);
    return cached;
  }
  const map = createDisplacementMap(width, height, radius);
  if (!map) return undefined;
  const canvas = document.createElement('canvas');
  canvas.width = map.width;
  canvas.height = map.height;
  const context = canvas.getContext('2d');
  if (!context) return undefined;
  const pixels = context.createImageData(map.width, map.height);
  pixels.data.set(map.data);
  context.putImageData(pixels, 0, 0);
  const entry = { url: canvas.toDataURL('image/png'), scale: map.scale };
  mapCache.set(key, entry);
  if (mapCache.size > 32) mapCache.delete(mapCache.keys().next().value as string);
  return entry;
};

const createFilter = (document: Document, width: number, height: number, radius: number) => {
  const map = getMap(document, width, height, radius);
  if (!map) return undefined;
  let root = roots.get(document);
  if (!root) {
    root = svgElement(document, 'svg', {
      'aria-hidden': 'true',
      'data-liquid-glass-defs': '',
      width: '0',
      height: '0',
      style: 'position:fixed;pointer-events:none;overflow:hidden',
    });
    document.body.append(root);
    roots.set(document, root);
  }
  const id = `liquid-glass-${nextId++}`;
  const filter = svgElement(document, 'filter', {
    id,
    filterUnits: 'userSpaceOnUse',
    primitiveUnits: 'userSpaceOnUse',
    x: '0',
    y: '0',
    width: String(width),
    height: String(height),
    'color-interpolation-filters': 'sRGB',
  });
  filter.append(
    svgElement(document, 'feGaussianBlur', {
      in: 'SourceGraphic',
      stdDeviation: '3',
      result: 'softened',
    }),
    svgElement(document, 'feImage', {
      href: map.url,
      x: '0',
      y: '0',
      width: String(width),
      height: String(height),
      preserveAspectRatio: 'none',
      result: 'displacement',
    })
  );
  // All three wavelengths share one map. A small scale separation colors the
  // curved rim without displacing foreground text or adding pointer work.
  ['red', 'green', 'blue'].forEach((channel, index) => {
    filter.append(
      svgElement(document, 'feDisplacementMap', {
        in: 'softened',
        in2: 'displacement',
        scale: String(map.scale * (0.98 + index * 0.02)),
        xChannelSelector: 'R',
        yChannelSelector: 'G',
        result: `${channel}-shift`,
      }),
      svgElement(document, 'feColorMatrix', {
        in: `${channel}-shift`,
        type: 'matrix',
        result: channel,
        values: Array.from({ length: 20 }, (_, cell) =>
          cell === index * 6 || cell === 18 ? '1' : '0'
        ).join(' '),
      })
    );
  });
  filter.append(
    svgElement(document, 'feBlend', {
      in: 'red',
      in2: 'green',
      mode: 'screen',
      result: 'red-green',
    }),
    svgElement(document, 'feBlend', { in: 'red-green', in2: 'blue', mode: 'screen' })
  );
  root.append(filter);
  const svgRoot = root;
  return {
    id,
    remove: () => {
      filter.remove();
      if (!svgRoot.childElementCount) {
        svgRoot.remove();
        roots.delete(document);
      }
    },
  };
};

/** Owns only the optical effect. Layout, tint, borders and states stay in CSS. */
export const attachLiquidGlass = (
  element: HTMLElement,
  { refraction = true }: { refraction?: boolean } = {}
): (() => void) => {
  const document = element.ownerDocument;
  const view = document.defaultView;
  if (
    !view ||
    typeof view.requestAnimationFrame !== 'function' ||
    typeof view.matchMedia !== 'function'
  )
    return () => {};

  const preferences = [
    view.matchMedia('(prefers-reduced-transparency: reduce)'),
    view.matchMedia('(prefers-contrast: more)'),
    view.matchMedia('(forced-colors: active)'),
    view.matchMedia('(prefers-reduced-motion: reduce)'),
  ];
  // CSS.supports accepts SVG filter URLs in engines that do not render them
  // as backdrop filters. Keep native blur on WebKit (including iOS) and Gecko.
  const supportsRefraction =
    refraction &&
    /(?:Chrome|Chromium)\/\d/.test(view.navigator.userAgent) &&
    typeof ResizeObserver === 'function' &&
    typeof IntersectionObserver === 'function' &&
    typeof CSS !== 'undefined' &&
    CSS.supports('backdrop-filter', 'url("#liquid-glass")');
  let disposed = false;
  let visible = false;
  let filter: ReturnType<typeof createFilter>;
  let dimensions = '';
  let mapFrame = 0;
  let lightFrame = 0;
  let pointerX = 0;
  let pointerY = 0;
  const permitsTransparency = () => !preferences.slice(0, 3).some((query) => query.matches);
  const permitsMotion = () => permitsTransparency() && !preferences[3].matches;

  const clearFilter = () => {
    element.style.removeProperty('--liquid-glass-filter');
    element.removeAttribute('data-liquid-glass');
    filter?.remove();
    filter = undefined;
    dimensions = '';
  };
  const clearLight = () => {
    view.cancelAnimationFrame(lightFrame);
    lightFrame = 0;
    element.style.removeProperty('--liquid-glass-light-x');
    element.style.removeProperty('--liquid-glass-light-y');
  };
  const updateMap = () => {
    mapFrame = 0;
    if (disposed || !visible || !permitsTransparency()) {
      clearFilter();
      return;
    }
    const width = element.offsetWidth;
    const height = element.offsetHeight;
    if (width <= 0 || height <= 0) {
      clearFilter();
      return;
    }
    const radiusValue = view.getComputedStyle(element).borderTopLeftRadius;
    const radius = Math.round(
      Math.min(
        width / 2,
        height / 2,
        radiusValue.endsWith('%')
          ? (Math.min(width, height) * parseFloat(radiusValue)) / 100
          : parseFloat(radiusValue) || 0
      )
    );
    const key = `${width}:${height}:${radius}`;
    if (dimensions === key) return;
    // A lost/blocked canvas or an unsupported SVG path must leave usable CSS glass.
    try {
      const nextFilter = createFilter(document, width, height, radius);
      clearFilter();
      filter = nextFilter;
      if (!filter) return;
      dimensions = key;
      element.style.setProperty('--liquid-glass-filter', `url("#${filter.id}")`);
      element.setAttribute('data-liquid-glass', 'active');
    } catch {
      clearFilter();
    }
  };
  const queueMap = () => {
    if (!mapFrame && supportsRefraction && !disposed)
      mapFrame = view.requestAnimationFrame(updateMap);
  };
  const onPreferenceChange = () => {
    if (disposed) return;
    if (!permitsTransparency()) clearFilter();
    if (!permitsMotion()) clearLight();
    queueMap();
  };
  const onPointerMove = (event: PointerEvent) => {
    if (!permitsMotion() || event.pointerType === 'touch') return;
    pointerX = event.clientX;
    pointerY = event.clientY;
    if (lightFrame) return;
    lightFrame = view.requestAnimationFrame(() => {
      lightFrame = 0;
      if (disposed || !permitsMotion()) return;
      const bounds = element.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      const x = Math.max(0, Math.min(100, ((pointerX - bounds.left) / bounds.width) * 100));
      const y = Math.max(0, Math.min(100, ((pointerY - bounds.top) / bounds.height) * 100));
      element.style.setProperty('--liquid-glass-light-x', `${x}%`);
      element.style.setProperty('--liquid-glass-light-y', `${y}%`);
    });
  };
  const resizeObserver = supportsRefraction ? new ResizeObserver(queueMap) : undefined;
  const intersectionObserver = supportsRefraction
    ? new IntersectionObserver((entries) => {
        if (disposed) return;
        visible = entries.some((entry) => entry.isIntersecting);
        if (visible) queueMap();
        else clearFilter();
      })
    : undefined;
  resizeObserver?.observe(element);
  intersectionObserver?.observe(element);
  preferences.forEach((query) => query.addEventListener?.('change', onPreferenceChange));
  element.addEventListener('pointermove', onPointerMove, { passive: true });
  element.addEventListener('pointerleave', clearLight);
  element.addEventListener('pointercancel', clearLight);

  return () => {
    disposed = true;
    view.cancelAnimationFrame(mapFrame);
    clearLight();
    clearFilter();
    resizeObserver?.disconnect();
    intersectionObserver?.disconnect();
    preferences.forEach((query) => query.removeEventListener?.('change', onPreferenceChange));
    element.removeEventListener('pointermove', onPointerMove);
    element.removeEventListener('pointerleave', clearLight);
    element.removeEventListener('pointercancel', clearLight);
  };
};
