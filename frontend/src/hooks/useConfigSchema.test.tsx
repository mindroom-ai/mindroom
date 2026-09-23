import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useConfigSchema } from "./useConfigSchema";

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}

// The hook caches the schema per module, so these cases run in order.
describe("useConfigSchema", () => {
  it("rejects a response that is not a configuration schema, then retries", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ has_key: false }));
    const failed = renderHook(() => useConfigSchema());
    await waitFor(() =>
      expect(failed.result.current.error).toBe(
        "Unexpected configuration schema response.",
      ),
    );
    expect(failed.result.current.schema).toBeNull();

    const schema = { type: "object", properties: {}, $defs: {} };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(schema));
    const loaded = renderHook(() => useConfigSchema());
    await waitFor(() => expect(loaded.result.current.schema).toEqual(schema));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("serves later callers from the cache", () => {
    const cached = renderHook(() => useConfigSchema());
    expect(cached.result.current.schema).toEqual({
      type: "object",
      properties: {},
      $defs: {},
    });
    expect(fetch).not.toHaveBeenCalled();
  });
});
