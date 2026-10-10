// Ported from MindRoom Chat (mindroom-ai/mindroom-chat src/app/components/glass/liquid/useLiquidGlass.test.tsx).
import React, { StrictMode, act, createRef } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { useGlassHighlight, useLiquidGlass } from './useLiquidGlass';
import { stubGlobal, unstubAllGlobals } from './stubGlobals';

describe.each(['liquid glass', 'highlight only'])('%s element lifecycle', (mode) => {
  const useGlass = mode === 'liquid glass' ? useLiquidGlass : useGlassHighlight;
  let container: HTMLDivElement;
  let root: Root;
  let motion: boolean;
  let preferenceListeners: Set<() => void>;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    motion = false;
    preferenceListeners = new Set();
    stubGlobal('matchMedia', (query: string) => ({
      get matches() {
        return query === '(prefers-reduced-motion: reduce)' && motion;
      },
      media: query,
      addEventListener: (_event: string, listener: () => void) => preferenceListeners.add(listener),
      removeEventListener: (_event: string, listener: () => void) =>
        preferenceListeners.delete(listener),
    }));
    stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(0), 0)
    );
    stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
    jest.useFakeTimers();
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    jest.useRealTimers();
    unstubAllGlobals();
  });

  const pointAt = (element: HTMLElement, x: number, y: number) => {
    jest.spyOn(element, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 200,
      bottom: 100,
      width: 200,
      height: 100,
      toJSON: () => ({}),
    });
    element.dispatchEvent(new MouseEvent('pointermove', { clientX: x, clientY: y }));
    jest.runAllTimers();
  };

  it('tracks the pointer after StrictMode effect replay and tears down detached elements', () => {
    const ref = createRef<HTMLDivElement>();
    let renderCount = 0;
    const Surface = () => {
      renderCount += 1;
      const glassRef = useGlass(ref);
      return <div ref={glassRef}>Clear text</div>;
    };
    act(() =>
      root.render(
        <StrictMode>
          <Surface />
        </StrictMode>
      )
    );
    const element = ref.current!;
    const rendersBeforePointer = renderCount;
    pointAt(element, 150, 25);
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('75%');
    expect(element.style.getPropertyValue('--liquid-glass-light-y')).toBe('25%');
    expect(renderCount).toBe(rendersBeforePointer);

    act(() => root.render(null));
    expect(ref.current).toBeNull();
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    pointAt(element, 25, 25);
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    expect(preferenceListeners.size).toBe(0);
  });

  it('resets the highlight when leaving and honors changing reduced-motion preferences', () => {
    const Surface = () => <div ref={useGlass()}>Clear text</div>;
    act(() => root.render(<Surface />));
    const element = container.firstElementChild as HTMLElement;
    pointAt(element, 150, 25);
    element.dispatchEvent(new MouseEvent('pointerleave'));
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    motion = true;
    preferenceListeners.forEach((listener) => listener());
    pointAt(element, 150, 25);
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    motion = false;
    preferenceListeners.forEach((listener) => listener());
    pointAt(element, 150, 25);
    expect(element.style.getPropertyValue('--liquid-glass-light-x')).toBe('75%');
  });

  it('skips disabled surfaces and releases listeners when toggled under StrictMode', () => {
    const ref = createRef<HTMLDivElement>();
    const Surface = ({ enabled }: { enabled: boolean }) => (
      <div ref={useGlass(ref, enabled)}>Clear text</div>
    );
    let element: HTMLDivElement | null = null;
    for (const enabled of [false, true, false, true]) {
      act(() =>
        root.render(
          <StrictMode>
            <Surface enabled={enabled} />
          </StrictMode>
        )
      );
      element ??= ref.current;
      expect(ref.current).toBe(element);
      pointAt(element!, 150, 25);
      expect(element!.style.getPropertyValue('--liquid-glass-light-x')).toBe(enabled ? '75%' : '');
      if (enabled) expect(preferenceListeners.size).toBeGreaterThan(0);
      else expect(preferenceListeners.size).toBe(0);
    }
    act(() => root.render(null));
    expect(ref.current).toBeNull();
    expect(preferenceListeners.size).toBe(0);
  });

  it('moves the effect and forwarded callback cleanly when React replaces the node', () => {
    const nodes: Array<HTMLElement | null> = [];
    const forwardedRef = (node: HTMLElement | null) => {
      nodes.push(node);
    };
    const Surface = ({ replace }: { replace: boolean }) => {
      const ref = useGlass(forwardedRef);
      return replace ? <section ref={ref} /> : <div ref={ref} />;
    };
    act(() => root.render(<Surface replace={false} />));
    const first = container.firstElementChild as HTMLElement;
    pointAt(first, 150, 25);
    act(() => root.render(<Surface replace />));
    const second = container.firstElementChild as HTMLElement;
    expect(nodes).toEqual([first, null, second]);
    expect(first.style.getPropertyValue('--liquid-glass-light-x')).toBe('');
    pointAt(second, 150, 25);
    expect(second.style.getPropertyValue('--liquid-glass-light-x')).toBe('75%');
  });
});
