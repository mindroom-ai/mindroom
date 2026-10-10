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

describe("requestConnection 403 details", () => {
  const messages = { forbidden: "You may not do that." };

  it("keeps the caller's own wording by default", async () => {
    reply({ detail: "Credential management is required" }, 403);
    await expect(
      requestConnection(
        "/x",
        new AbortController().signal,
        "GET",
        {},
        messages,
      ),
    ).rejects.toThrow("You may not do that.");
  });

  it("shows the server's reason when asked to, with the wording as the fallback", async () => {
    reply({ detail: "Credential management is required" }, 403);
    const asked = { ...messages, forbiddenDetail: true };
    await expect(
      requestConnection("/x", new AbortController().signal, "GET", {}, asked),
    ).rejects.toThrow("Credential management is required");
    for (const body of [{}, { detail: "" }, { detail: [{ msg: "x" }] }]) {
      reply(body, 403);
      await expect(
        requestConnection("/x", new AbortController().signal, "GET", {}, asked),
      ).rejects.toThrow("You may not do that.");
    }
    vi.mocked(fetch).mockResolvedValue(new Response("<html>", { status: 403 }));
    await expect(
      requestConnection("/x", new AbortController().signal, "GET", {}, asked),
    ).rejects.toThrow("You may not do that.");
    reply({ detail: "Credential management is required" }, 403);
    await expect(
      requestConnection(
        "/x",
        new AbortController().signal,
        "GET",
        {},
        { forbiddenDetail: true },
      ),
    ).rejects.toThrow("Credential management is required");
    reply({}, 403);
    await expect(
      requestConnection(
        "/x",
        new AbortController().signal,
        "GET",
        {},
        { forbiddenDetail: true },
      ),
    ).rejects.toThrow("Connections are not available for this account.");
  });
});
