import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  confirmLeavingUnsavedDrafts,
  useUnsavedDraft,
} from "./useUnsavedDraft";

describe("useUnsavedDraft", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("asks only while a mounted editor holds an unsaved draft", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { rerender, unmount } = renderHook(
      ({ isDirty }) => useUnsavedDraft(isDirty),
      { initialProps: { isDirty: false } },
    );

    expect(confirmLeavingUnsavedDrafts()).toBe(true);

    rerender({ isDirty: true });
    expect(confirmLeavingUnsavedDrafts()).toBe(false);
    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");

    rerender({ isDirty: false });
    expect(confirmLeavingUnsavedDrafts()).toBe(true);

    rerender({ isDirty: true });
    unmount();
    expect(confirmLeavingUnsavedDrafts()).toBe(true);
  });

  it("makes reloading or closing the tab ask only while a mounted editor holds an unsaved draft", () => {
    // True when the browser would show its leave-page prompt.
    const unloadAsks = () =>
      !window.dispatchEvent(new Event("beforeunload", { cancelable: true }));
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const first = renderHook(({ isDirty }) => useUnsavedDraft(isDirty), {
      initialProps: { isDirty: false },
    });
    const second = renderHook(({ isDirty }) => useUnsavedDraft(isDirty), {
      initialProps: { isDirty: false },
    });

    expect(unloadAsks()).toBe(false);

    first.rerender({ isDirty: true });
    second.rerender({ isDirty: true });
    expect(unloadAsks()).toBe(true);

    first.rerender({ isDirty: false });
    expect(unloadAsks()).toBe(true);

    second.unmount();
    expect(unloadAsks()).toBe(false);
    const added = addSpy.mock.calls.filter(([type]) => type === "beforeunload");
    const removed = removeSpy.mock.calls.filter(
      ([type]) => type === "beforeunload",
    );
    // Nothing stays registered, so the browser keeps the page cacheable.
    expect(added).not.toHaveLength(0);
    expect(removed.map(([, listener]) => listener)).toEqual(
      expect.arrayContaining(added.map(([, listener]) => listener)),
    );
  });

  it("also sets returnValue, which Chrome and Edge before 119 need to show the leave-page prompt", () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const { unmount } = renderHook(() => useUnsavedDraft(true));
    const call = addSpy.mock.calls.find(([type]) => type === "beforeunload");
    expect(call).toBeDefined();
    const listener = call?.[1] as (event: BeforeUnloadEvent) => void;
    const event = {
      preventDefault: vi.fn(),
      returnValue: undefined,
    } as unknown as BeforeUnloadEvent;

    listener(event);

    expect(event.preventDefault).toHaveBeenCalled();
    expect(event.returnValue).toBe(true);
    unmount();
  });

  it("asks before an editor's own action only when its draft is unsaved", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const clean = renderHook(() => useUnsavedDraft(false));
    const dirty = renderHook(() => useUnsavedDraft(true));

    expect(clean.result.current()).toBe(true);

    expect(dirty.result.current()).toBe(false);
    confirmSpy.mockReturnValue(true);
    expect(dirty.result.current()).toBe(true);
    expect(confirmSpy).toHaveBeenCalledTimes(2);
  });
});
