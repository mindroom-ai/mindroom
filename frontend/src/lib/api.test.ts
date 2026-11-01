import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchJSON } from "./api";

describe("fetchJSON", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    {
      body: JSON.stringify({ detail: "Agent not found" }),
      message: "Agent not found",
    },
    {
      body: "<html>Bad gateway</html>",
      message: "API call failed: 502 Bad Gateway",
    },
  ])("reports a failed request as $message", async ({ body, message }) => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(body, { status: 502, statusText: "Bad Gateway" }),
        ),
    );

    await expect(fetchJSON("/api/agents")).rejects.toThrow(message);
  });
});
