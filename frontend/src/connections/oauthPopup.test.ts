/// <reference types="vitest/jsdom" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectWithPopup } from "./oauthPopup";

const authorization = {
  provider: "mail",
  auth_url: "https://auth.example.com/start",
  completion_origin: "https://portal.example.com",
};
const initialPageUrl = window.location.href;

describe("connection popup", () => {
  let popup: {
    closed: boolean;
    close: ReturnType<typeof vi.fn>;
    location: { href: string };
  };
  let controller: AbortController;
  beforeEach(() => {
    vi.useFakeTimers();
    popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
    };
    controller = new AbortController();
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  });
  afterEach(() => {
    jsdom.reconfigure({ url: initialPageUrl });
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function complete(overrides: MessageEventInit = {}) {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: authorization.completion_origin,
        source: popup as unknown as Window,
        data: {
          type: "mindroom:oauth-complete",
          provider: "mail",
          status: "connected",
        },
        ...overrides,
      }),
    );
  }

  it("ignores wrong origin, source, and provider before accepting completion", async () => {
    const operation = connectWithPopup(
      "mail",
      async () => authorization,
      controller.signal,
    );
    await Promise.resolve();
    complete({ origin: "https://untrusted.example.com" });
    complete({ source: window });
    complete({
      data: {
        type: "mindroom:oauth-complete",
        provider: "other",
        status: "connected",
      },
    });
    expect(popup.close).not.toHaveBeenCalled();
    expect(popup.location.href).toBe(authorization.auth_url);
    complete();
    await expect(operation).resolves.toBeUndefined();
    expect(popup.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects blocked popup without starting authorization", async () => {
    vi.mocked(window.open).mockReturnValue(null);
    const authorize = vi.fn(async () => authorization);
    await expect(
      connectWithPopup("mail", authorize, controller.signal),
    ).rejects.toThrow(/popup.*blocked/i);
    expect(authorize).not.toHaveBeenCalled();
  });

  it("closes popup and removes listeners when authorization fails", async () => {
    await expect(
      connectWithPopup(
        "mail",
        async () => {
          throw new Error("Could not start connection.");
        },
        controller.signal,
      ),
    ).rejects.toThrow("Could not start connection.");
    complete();
    expect(popup.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels when popup closes", async () => {
    const operation = connectWithPopup(
      "mail",
      async () => authorization,
      controller.signal,
    );
    const rejection = expect(operation).rejects.toThrow(/cancelled/i);
    await Promise.resolve();
    popup.closed = true;
    await vi.advanceTimersByTimeAsync(1000);
    await rejection;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts an outstanding authorization request and ignores its late response", async () => {
    let finish!: (value: typeof authorization) => void;
    const operation = connectWithPopup(
      "mail",
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
      controller.signal,
    );
    const rejection = expect(operation).rejects.toThrow(/cancelled/i);
    controller.abort();
    await rejection;
    finish(authorization);
    await Promise.resolve();
    expect(popup.location.href).toBe("about:blank");
    expect(popup.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out an open popup and releases every flow resource", async () => {
    const removeMessageListener = vi.spyOn(window, "removeEventListener");
    const removeAbortListener = vi.spyOn(
      controller.signal,
      "removeEventListener",
    );
    const settled = vi.fn();
    const operation = connectWithPopup(
      "mail",
      async () => authorization,
      controller.signal,
    ).then(() => settled("connected"), settled);
    try {
      await vi.advanceTimersByTimeAsync(299_999);
      expect(settled).not.toHaveBeenCalled();
      expect(popup.location.href).toBe(authorization.auth_url);
      await vi.advanceTimersByTimeAsync(1);
      expect(settled).toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringMatching(/timed out/i),
        }),
      );
      expect(popup.close).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
      expect(removeMessageListener).toHaveBeenCalledWith(
        "message",
        expect.any(Function),
      );
      expect(removeAbortListener).toHaveBeenCalledWith(
        "abort",
        expect.any(Function),
      );
      complete();
      controller.abort();
      expect(popup.close).toHaveBeenCalledOnce();
      expect(settled).toHaveBeenCalledOnce();
    } finally {
      controller.abort();
      await operation;
    }
  });

  it("ignores an authorization response arriving after the flow deadline", async () => {
    let authorize!: (value: typeof authorization) => void;
    const settled = vi.fn();
    const operation = connectWithPopup(
      "mail",
      () =>
        new Promise((resolve) => {
          authorize = resolve;
        }),
      controller.signal,
    ).then(() => settled("connected"), settled);
    try {
      await vi.advanceTimersByTimeAsync(300_000);
      expect(settled).toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringMatching(/timed out/i),
        }),
      );
      authorize(authorization);
      await Promise.resolve();
      expect(popup.location.href).toBe("about:blank");
      expect(popup.close).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      controller.abort();
      await operation;
    }
  });

  it.each([
    ["http://localhost:3000", "auth_url", "http://auth.example.com/start"],
    ["http://localhost:3000", "completion_origin", "http://portal.example.com"],
    ["https://portal.example.com", "auth_url", "http://localhost/start"],
    ["https://portal.example.com", "completion_origin", "http://localhost"],
    ["https://localhost", "auth_url", "http://localhost/start"],
    ["http://portal.example.com", "auth_url", "http://localhost/start"],
    ["http://localhost:3000", "auth_url", "http://localhost.example.com/start"],
    ["http://localhost:3000", "auth_url", "http://127.0.0.1.example.com/start"],
    ["http://localhost:3000", "auth_url", "javascript:alert(1)"],
    ["http://localhost:3000", "completion_origin", "data:text/html,hello"],
  ])(
    "rejects %s portal's unsafe %s %s before navigating",
    async (pageUrl, field, targetUrl) => {
      jsdom.reconfigure({ url: pageUrl });
      const settled = vi.fn();
      const operation = connectWithPopup(
        "mail",
        async () => ({ ...authorization, [field]: targetUrl }),
        controller.signal,
      ).then(() => settled("connected"), settled);
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(settled).toHaveBeenCalledWith(
          expect.objectContaining({
            message: expect.stringMatching(/could not start/i),
          }),
        );
        expect(popup.location.href).toBe("about:blank");
        expect(popup.close).toHaveBeenCalledOnce();
        expect(vi.getTimerCount()).toBe(0);
      } finally {
        controller.abort();
        await operation;
      }
    },
  );

  it.each(["localhost", "127.8.2.1", "[::1]", "app.localhost"])(
    "allows HTTP loopback targets when the portal runs on HTTP %s",
    async (hostname) => {
      jsdom.reconfigure({ url: `http://${hostname}:3000/connections/` });
      const authUrl = `http://${hostname}:8765/start`;
      const completionOrigin = `http://${hostname}:8765`;
      const operation = connectWithPopup(
        "mail",
        async () => ({
          provider: "mail",
          auth_url: authUrl,
          completion_origin: completionOrigin,
        }),
        controller.signal,
      );
      await Promise.resolve();
      expect(popup.location.href).toBe(authUrl);
      complete({ origin: completionOrigin });
      await expect(operation).resolves.toBeUndefined();
      expect(popup.close).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
    },
  );
});
