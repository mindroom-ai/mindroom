import { afterEach, describe, expect, it, vi } from "vitest";
import { requestConnection } from "./request";

const reply = (body: unknown, status: number) =>
  vi
    .mocked(fetch)
    .mockResolvedValue(new Response(JSON.stringify(body), { status }));
const request = () =>
  requestConnection("/api/connections/egress", new AbortController().signal);

afterEach(() => vi.restoreAllMocks());

describe("requestConnection error details", () => {
  it.each([409, 413, 422])(
    "shows the string detail of a %i",
    async (status) => {
      reply({ detail: "Something the user can act on" }, status);
      await expect(request()).rejects.toThrow("Something the user can act on");
    },
  );

  it("joins FastAPI's list shape, drops the body prefix, and never reads the input", async () => {
    reply(
      {
        detail: [
          {
            loc: ["body", "rules", 0, "host"],
            msg: "Input should be a valid string",
            input: "SECRET-INPUT",
          },
          { loc: ["body"], msg: "Field required" },
          { loc: ["query", "limit"], msg: "Input should be a valid integer" },
          { loc: [], msg: "Invalid request" },
        ],
      },
      422,
    );
    await expect(request()).rejects.toThrow(
      "rules.0.host: Input should be a valid string; Field required; query.limit: Input should be a valid integer; Invalid request",
    );
  });

  it.each([
    ["an empty string", ""],
    ["an object", { code: "x" }],
    ["a list without messages", [{ loc: ["body"] }, "text", null]],
    ["nothing", undefined],
  ])("falls back to the general message for %s", async (_name, detail) => {
    reply({ detail }, 409);
    await expect(request()).rejects.toThrow(
      "Could not complete the request. Try again.",
    );
  });

  it("falls back when the error body is not JSON", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("<html>", { status: 413 }));
    await expect(request()).rejects.toThrow(
      "Could not complete the request. Try again.",
    );
  });

  it("does not show the detail of other statuses", async () => {
    reply({ detail: "stack trace" }, 500);
    await expect(request()).rejects.toThrow(
      "Could not complete the request. Try again.",
    );
  });
});
