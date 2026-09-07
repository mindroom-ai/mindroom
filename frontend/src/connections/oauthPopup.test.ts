import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectWithPopup } from "./oauthPopup";

const authorization = {
  provider: "mail",
  auth_url: "https://auth.example.com/start",
  completion_origin: "https://portal.example.com",
};

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
});
