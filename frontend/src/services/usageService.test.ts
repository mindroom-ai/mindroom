import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchUsage } from "./usageService";

const totals = {
  input_tokens: 10,
  output_tokens: 5,
  total_tokens: 15,
  cache_read_tokens: 2,
  cache_write_tokens: 1,
  reasoning_tokens: 3,
  audio_input_tokens: 0,
  audio_output_tokens: 0,
  audio_total_tokens: 0,
};

const coverage = {
  scanned_sources: 1,
  unavailable_sources: 0,
  note: "Retained usage from available sources.",
};

const report = {
  schema_version: 1,
  generated_at: "2026-09-19T12:00:00Z",
  scope: "admin",
  totals,
  session_count: 1,
  breakdown: [
    {
      dimension: "entity",
      key: "assistant",
      totals,
      session_count: 1,
      cumulative_model_breakdown: [
        {
          provider: "example",
          model: "example-model",
          totals,
          session_count: 1,
        },
      ],
      retained_run_totals: totals,
      run_count: 1,
      user_breakdown: [
        {
          user_id: "user@example.test",
          totals,
          run_count: 1,
          model_breakdown: [
            {
              provider: "example",
              model: "example-model",
              totals,
              run_count: 1,
            },
          ],
          daily_breakdown: [],
        },
      ],
    },
  ],
  coverage,
  model_breakdown: [],
  model_coverage: coverage,
  cumulative_model_breakdown: [],
  cumulative_model_coverage: coverage,
  user_breakdown: [],
  user_coverage: coverage,
  daily_breakdown: [],
  daily_coverage: coverage,
};

describe("fetchUsage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns a ready report using dashboard cookies and the supplied signal", async () => {
    const controller = new AbortController();
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(fetchUsage(controller.signal)).resolves.toEqual({
      status: "ready",
      report,
    });
    expect(fetch).toHaveBeenCalledWith("/api/usage?include_daily=true", {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
  });

  it.each([
    ["3", 3_000],
    [null, 5_000],
    ["0.1", 1_000],
    ["60", 30_000],
    ["not-a-number", 5_000],
  ])(
    "returns a bounded pending delay for Retry-After %s",
    async (retryAfter, expectedMs) => {
      const headers =
        retryAfter == null ? undefined : { "Retry-After": retryAfter };
      vi.mocked(fetch).mockResolvedValue(
        new Response(JSON.stringify({ status: "pending" }), {
          status: 202,
          headers,
        }),
      );

      await expect(fetchUsage()).resolves.toEqual({
        status: "pending",
        retryAfterMs: expectedMs,
      });
    },
  );

  it.each([
    [401, "Your session has expired. Reload this page to sign in again."],
    [403, "You do not have permission to view usage data."],
    [503, "Usage data is temporarily unavailable. Try again."],
  ])("returns a safe error for HTTP %i", async (status, message) => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("sensitive detail", { status }),
    );

    await expect(fetchUsage()).rejects.toThrow(message);
  });

  it("returns a safe error when the server cannot be reached", async () => {
    vi.mocked(fetch).mockRejectedValue(
      new TypeError("internal network detail"),
    );

    await expect(fetchUsage()).rejects.toThrow(
      "Could not reach the server. Try again.",
    );
  });

  it("returns a safe error for a non-JSON success response", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("not json", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      }),
    );

    await expect(fetchUsage()).rejects.toThrow(
      "Could not read usage data from the server. Try again.",
    );
  });

  it("rejects a JSON response that is not an admin usage report", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          ...report,
          scope: "self",
        }),
        { status: 200 },
      ),
    );

    await expect(fetchUsage()).rejects.toThrow(
      "Could not read usage data from the server. Try again.",
    );
  });

  it.each([
    ["entity totals", { breakdown: [{}] }],
    [
      "entity requester rows",
      { breakdown: [{ ...report.breakdown[0], user_breakdown: [{}] }] },
    ],
    [
      "model totals",
      {
        cumulative_model_breakdown: [
          { provider: "example", model: "sample", session_count: 1 },
        ],
      },
    ],
    [
      "requester ID",
      {
        user_breakdown: [
          { ...report.breakdown[0].user_breakdown[0], user_id: {} },
        ],
      },
    ],
    [
      "daily date",
      { daily_breakdown: [{ date: "invalid", totals, run_count: 1 }] },
    ],
    ["report timestamp", { generated_at: "invalid" }],
    ["unsafe total", { totals: { ...totals, input_tokens: 9007199254740992 } }],
    [
      "unsafe nested total",
      {
        user_breakdown: [
          {
            ...report.breakdown[0].user_breakdown[0],
            totals: { ...totals, cache_read_tokens: 9007199254740992 },
          },
        ],
      },
    ],
  ])("rejects malformed %s before rendering", async (_name, fields) => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ ...report, ...fields })),
    );
    await expect(fetchUsage()).rejects.toThrow(
      "Could not read usage data from the server. Try again.",
    );
  });

  it("preserves request cancellation", async () => {
    const cancellation = new DOMException(
      "The operation was aborted.",
      "AbortError",
    );
    vi.mocked(fetch).mockRejectedValue(cancellation);

    await expect(fetchUsage()).rejects.toBe(cancellation);
  });
});
