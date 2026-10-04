// Ported from MindRoom Chat (mindroom-ai/mindroom-chat src/app/components/glass/liquid/liquidGlass.test.ts).
import { attachLiquidGlass } from './liquidGlass';
import { stubGlobal, unstubAllGlobals } from './stubGlobals';

describe('liquid glass rendering lifecycle', () => {
  let element: HTMLDivElement;
  let cleanup: () => void;
  let width: number;
  let notifyResize: ResizeObserverCallback;
  let notifyVisibility: IntersectionObserverCallback;
  let preferences: Map<string, boolean>;
  let preferenceListeners: Set<() => void>;

  const show = (isIntersecting: boolean) => {
    notifyVisibility(
      [{ target: element, isIntersecting } as unknown as IntersectionObserverEntry],
      {} as IntersectionObserver
    );
    jest.runAllTimers();
  };
  const setPreference = (query: string, enabled: boolean) => {
    preferences.set(query, enabled);
    preferenceListeners.forEach((listener) => listener());
    jest.runAllTimers();
  };

  beforeEach(() => {
    preferences = new Map();
    preferenceListeners = new Set();
    width = 180;
    cleanup = () => {};
    jest.useFakeTimers();
    jest.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36'
    );
    stubGlobal('CSS', { supports: () => true });
    stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(0), 0)
    );
    stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
    stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: ResizeObserverCallback) {
          notifyResize = callback;
        }

        observe() {}

        disconnect() {}
      }
    );
    stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          notifyVisibility = callback;
        }

        observe() {}

        disconnect() {}
      }
    );
    stubGlobal('matchMedia', (query: string) => ({
      get matches() {
        return preferences.get(query) ?? false;
      },
      media: query,
      addEventListener: (_event: string, listener: () => void) => preferenceListeners.add(listener),
      removeEventListener: (_event: string, listener: () => void) =>
        preferenceListeners.delete(listener),
    }));
    // JSDOM has no rasterizer; geometry is covered with real bytes separately.
    jest.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
      putImageData: () => {},
    } as unknown as CanvasRenderingContext2D);
    jest.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(
      'data:image/png;base64,AA=='
    );
    element = document.createElement('div');
    element.style.borderRadius = '20px';
    Object.defineProperties(element, {
      offsetWidth: { get: () => width },
      offsetHeight: { value: 80 },
    });
    document.body.append(element);
  });

  afterEach(() => {
    cleanup();
    element.remove();
    jest.restoreAllMocks();
    unstubAllGlobals();
    jest.useRealTimers();
  });

  it('tracks highlights without allocating optical observers when refraction is disabled', () => {
    const resize = jest.fn(() => ({ observe() {}, disconnect() {} }));
    const intersection = jest.fn(() => ({ observe() {}, disconnect() {} }));
    stubGlobal('ResizeObserver', resize);
    stubGlobal('IntersectionObserver', intersection);
    jest.spyOn(element, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      width: 200,
      height: 100,
    } as DOMRect);
    cleanup = attachLiquidGlass(element, { refraction: false });
    element.dispatchEvent(new MouseEvent('pointermove', { clientX: 150, clientY: 25 }));
    jest.runAllTimers();
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('75%');
    expect(element.style.getPropertyValue('--liquid-glass-light-y')).toBe('25%');
    expect(resize).not.toHaveBeenCalled();
    expect(intersection).not.toHaveBeenCalled();
    expect(element.hasAttribute('data-liquid-glass')).toBe(false);
    setPreference('(prefers-reduced-motion: reduce)', true);
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    cleanup();
    expect(preferenceListeners.size).toBe(0);
  });

  it('defers hidden surfaces, resizes their optical field, and releases hidden filters', () => {
    cleanup = attachLiquidGlass(element);
    notifyResize([], {} as ResizeObserver);
    jest.runAllTimers();
    expect(element.hasAttribute('data-liquid-glass')).toBe(false);
    expect(document.querySelector('filter')).toBeNull();
    show(true);
    expect(element.getAttribute('data-liquid-glass')).toBe('active');
    const firstFilter = document.querySelector('filter')!;
    expect(firstFilter.getAttribute('width')).toBe('180');
    width = 320;
    notifyResize([], {} as ResizeObserver);
    jest.runAllTimers();
    expect(firstFilter.isConnected).toBe(false);
    expect(document.querySelector('filter')?.getAttribute('width')).toBe('320');
    expect(document.querySelector('feImage')?.getAttribute('width')).toBe('320');
    show(false);
    expect(element.style.getPropertyValue('--liquid-glass-filter')).toBe('');
    expect(document.querySelector('[data-liquid-glass-defs]')).toBeNull();
  });

  it.each([
    '(prefers-reduced-transparency: reduce)',
    '(prefers-contrast: more)',
    '(forced-colors: active)',
  ])('removes and restores the optical effect when %s changes', (query) => {
    cleanup = attachLiquidGlass(element);
    show(true);
    setPreference(query, true);
    expect(element.hasAttribute('data-liquid-glass')).toBe(false);
    expect(document.querySelector('filter')).toBeNull();
    setPreference(query, false);
    expect(element.getAttribute('data-liquid-glass')).toBe('active');
    expect(document.querySelectorAll('filter')).toHaveLength(1);
    cleanup();
    expect(element.hasAttribute('data-liquid-glass')).toBe(false);
    expect(document.querySelector('[data-liquid-glass-defs]')).toBeNull();
    expect(preferenceListeners.size).toBe(0);
  });

  it('keeps native CSS fallback on Safari even when CSS.supports accepts filter URLs', () => {
    jest.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 Version/18.0 Safari/605.1.15'
    );
    cleanup = attachLiquidGlass(element);
    jest.runAllTimers();
    expect(element.hasAttribute('data-liquid-glass')).toBe(false);
    expect(document.querySelector('filter')).toBeNull();
  });

  it('splits and recombines nearby optical scales for chromatic rim separation', () => {
    cleanup = attachLiquidGlass(element);
    show(true);
    const displacements = [...document.querySelectorAll('feDisplacementMap')];
    expect(displacements).toHaveLength(3);
    const scales = displacements.map((node) => Number(node.getAttribute('scale')));
    expect(scales[0]).toBeLessThan(scales[1]);
    expect(scales[2]).toBeGreaterThan(scales[1]);
    expect(scales[2] / scales[0]).toBeLessThan(1.05);
    expect(document.querySelectorAll('feImage')).toHaveLength(1);
    expect(document.querySelectorAll('feBlend')).toHaveLength(2);
  });
});
