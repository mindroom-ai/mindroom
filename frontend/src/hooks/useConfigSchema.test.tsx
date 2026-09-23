import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const SCHEMA = { type: "object", properties: {}, $defs: {} };

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}

// The hook caches per module, so each test loads a fresh module instance.
async function loadHook() {
  vi.resetModules();
  return (await import("./useConfigSchema")).useConfigSchema;
}

describe("useConfigSchema", () => {
  beforeEach(() => {
    vi.mocked(fetch).mockReset();
  });

  it("rejects a response that is not a configuration schema and retries on request", async () => {
    const useConfigSchema = await loadHook();
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ has_key: false }));
    const { result } = renderHook(() => useConfigSchema());
    await waitFor(() =>
      expect(result.current.error).toBe(
        "Unexpected configuration schema response.",
      ),
    );
    expect(result.current.schema).toBeNull();

    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(SCHEMA));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.schema).toEqual(SCHEMA));
    expect(result.current.error).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("serves later callers from the cache", async () => {
    const useConfigSchema = await loadHook();
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(SCHEMA));
    const first = renderHook(() => useConfigSchema());
    await waitFor(() => expect(first.result.current.schema).toEqual(SCHEMA));

    const later = renderHook(() => useConfigSchema());
    expect(later.result.current.schema).toEqual(SCHEMA);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
