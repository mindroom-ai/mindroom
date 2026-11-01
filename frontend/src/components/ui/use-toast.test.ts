import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useToast } from "./use-toast";

describe("useToast", () => {
  it("returns the same toast function on every render", () => {
    // Callbacks and effects list `toast` as a dependency, so a new function
    // per render would rerun them on every render.
    const { result, rerender } = renderHook(() => useToast());
    const first = result.current.toast;

    rerender();

    expect(result.current.toast).toBe(first);
  });
});
