import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestOpenIdFromOpener, signInWithMatrix } from "./matrixSignIn";

const openidToken = {
  access_token: "syt_token",
  token_type: "Bearer",
  matrix_server_name: "example.org",
  expires_in: 3600,
};
const chatOrigin = "https://chat.example.org";

function makeWindow(opener: unknown = { postMessage: vi.fn() }) {
  return Object.assign(new EventTarget(), { opener }) as unknown as Window & {
    opener: { postMessage: ReturnType<typeof vi.fn> } | null;
  };
}

function reply(
  win: Window,
  overrides: { source?: unknown; origin?: string; data?: unknown } = {},
) {
  win.dispatchEvent(
    new MessageEvent("message", {
      origin: chatOrigin,
      source: win.opener as MessageEventSource,
      data: { type: "mindroom:connections-openid", openid_token: openidToken },
      ...overrides,
    } as MessageEventInit),
  );
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("requestOpenIdFromOpener", () => {
  it("returns token and origin from the opener", async () => {
    const win = makeWindow();
    const pending = requestOpenIdFromOpener(win);
    expect(win.opener!.postMessage).toHaveBeenCalledExactlyOnceWith(
      { type: "mindroom:connections-ready" },
      "*",
    );
    reply(win);
    await expect(pending).resolves.toEqual({
      openidToken,
      origin: chatOrigin,
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores messages from other sources", async () => {
    const win = makeWindow();
    const pending = requestOpenIdFromOpener(win);
    reply(win, { source: { postMessage: vi.fn() } });
    reply(win, { source: null });
    await vi.advanceTimersByTimeAsync(29_999);
    reply(win);
    await expect(pending).resolves.toEqual({
      openidToken,
      origin: chatOrigin,
    });
  });

  it("ignores messages with another type", async () => {
    const win = makeWindow();
    const pending = requestOpenIdFromOpener(win);
    for (const data of [
      null,
      "mindroom:connections-openid",
      { type: "mindroom:connections-ready", openid_token: openidToken },
      { type: "mindroom:connections-openid" },
      { type: "mindroom:connections-openid", openid_token: "syt_token" },
      { type: "mindroom:connections-openid", openid_token: null },
    ]) {
      reply(win, { data });
    }
    const settled = vi.fn();
    void pending.then(settled);
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).not.toHaveBeenCalled();
    reply(win);
    await expect(pending).resolves.toMatchObject({ origin: chatOrigin });
  });

  it("returns null without an opener", async () => {
    const win = makeWindow(null);
    const addListener = vi.spyOn(win, "addEventListener");
    await expect(requestOpenIdFromOpener(win)).resolves.toBeNull();
    expect(addListener).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("returns null after the timeout", async () => {
    const win = makeWindow();
    const removeListener = vi.spyOn(win, "removeEventListener");
    const pending = requestOpenIdFromOpener(win);
    const settled = vi.fn();
    void pending.then(settled);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await expect(pending).resolves.toBeNull();
    expect(removeListener).toHaveBeenCalledWith(
      "message",
      expect.any(Function),
    );
    expect(vi.getTimerCount()).toBe(0);
    const late = vi.fn();
    void pending.then(late);
    reply(win);
    expect(late).toHaveBeenCalledTimes(0);
  });

  it("stops waiting and removes its listener when aborted", async () => {
    const win = makeWindow();
    const removeListener = vi.spyOn(win, "removeEventListener");
    const controller = new AbortController();
    const pending = requestOpenIdFromOpener(win, 30_000, controller.signal);
    controller.abort();
    await expect(pending).resolves.toBeNull();
    expect(removeListener).toHaveBeenCalledWith(
      "message",
      expect.any(Function),
    );
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not post to the opener when already aborted", async () => {
    const win = makeWindow();
    const controller = new AbortController();
    controller.abort();
    await expect(
      requestOpenIdFromOpener(win, 30_000, controller.signal),
    ).resolves.toBeNull();
    expect(win.opener!.postMessage).not.toHaveBeenCalled();
  });
});

describe("signInWithMatrix", () => {
  beforeEach(() => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 200 }));
  });

  it("posts token and client origin to the session endpoint", async () => {
    const win = makeWindow();
    const pending = signInWithMatrix(win);
    reply(win);
    await expect(pending).resolves.toBe(true);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/connections/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        openid_token: openidToken,
        client_origin: chatOrigin,
      }),
    });
  });

  it("returns false and skips the request without a token", async () => {
    const win = makeWindow(null);
    await expect(signInWithMatrix(win)).resolves.toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns false when the server rejects the token", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 403 }));
    const win = makeWindow();
    const pending = signInWithMatrix(win);
    reply(win);
    await expect(pending).resolves.toBe(false);
  });

  it("returns false when the request cannot reach the server", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));
    const win = makeWindow();
    const pending = signInWithMatrix(win);
    reply(win);
    await expect(pending).resolves.toBe(false);
  });
});
