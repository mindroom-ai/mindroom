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
