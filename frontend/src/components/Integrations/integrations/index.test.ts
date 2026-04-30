import { beforeEach, describe, expect, it, vi } from "vitest";
import { integrationProviders } from "./index";

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  withAgentExecutionScope: (url: string) => url,
}));

global.fetch = vi.fn();
global.window.open = vi.fn();

describe("Generic OAuth integration provider", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it("resolves connect when the OAuth popup posts a completion message", async () => {
    const authWindowState = { closed: false };
    const authWindow = {
      get closed() {
        return authWindowState.closed;
      },
      close: vi.fn(() => {
        authWindowState.closed = true;
      }),
    } as unknown as Window;
    (global.window.open as any).mockReturnValue(authWindow);
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
    });

    const config = integrationProviders.google_drive.getConfig();
    const connectPromise = config.onAction!(config.integration);

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom:oauth-complete",
          provider: "google_drive",
          status: "connected",
        },
        source: authWindow,
        origin: window.location.origin,
      }),
    );

    await connectPromise;

    expect(authWindow.close).toHaveBeenCalled();
  });

  it("ignores OAuth completion messages from other origins", async () => {
    vi.useFakeTimers();
    const authWindowState = { closed: false };
    const authWindow = {
      get closed() {
        return authWindowState.closed;
      },
      close: vi.fn(() => {
        authWindowState.closed = true;
      }),
    } as unknown as Window;
    (global.window.open as any).mockReturnValue(authWindow);
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
    });

    const config = integrationProviders.google_drive.getConfig();
    const connectPromise = config.onAction!(config.integration);

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom:oauth-complete",
          provider: "google_drive",
          status: "connected",
        },
        source: authWindow,
        origin: "https://evil.example.test",
      }),
    );

    const rejection = expect(connectPromise).rejects.toThrow(
      "Google Drive authorization was cancelled",
    );
    authWindowState.closed = true;
    await vi.advanceTimersByTimeAsync(1000);

    await rejection;
    expect(authWindow.close).not.toHaveBeenCalled();
  });

  it("rejects connect when the OAuth popup closes without completion", async () => {
    vi.useFakeTimers();
    const authWindowState = { closed: false };
    const authWindow = {
      get closed() {
        return authWindowState.closed;
      },
      close: vi.fn(() => {
        authWindowState.closed = true;
      }),
    } as unknown as Window;
    (global.window.open as any).mockReturnValue(authWindow);
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
    });

    const config = integrationProviders.google_drive.getConfig();
    const connectPromise = config.onAction!(config.integration);

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    const rejection = expect(connectPromise).rejects.toThrow(
      "Google Drive authorization was cancelled",
    );
    authWindowState.closed = true;
    await vi.advanceTimersByTimeAsync(1000);

    await rejection;
  });

  it("fails closed when OAuth status fails", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: "server error" }),
    });

    const status = await integrationProviders.google_drive.loadStatus!();

    expect(status).toMatchObject({
      status: "not_connected",
      connected: false,
      oauth_client_configured: false,
    });
  });

  it("fails closed when OAuth status cannot be loaded", async () => {
    (global.fetch as any).mockRejectedValueOnce(new Error("network error"));

    const status = await integrationProviders.google_drive.loadStatus!();

    expect(status).toMatchObject({
      status: "not_connected",
      connected: false,
      oauth_client_configured: false,
    });
  });
});
