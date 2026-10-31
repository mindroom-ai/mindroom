import { isValidElement } from "react";
import {
  SiGmail,
  SiGooglecalendar,
  SiGooglecloud,
  SiGoogledocs,
  SiGoogledrive,
  SiGooglesheets,
  SiGoogletasks,
  SiHomeassistant,
} from "react-icons/si";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GenericOAuthIntegrationProvider, builtInProviderCards } from "./index";

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  withAgentExecutionScope: (
    url: string,
    agentName?: string | null,
    executionScope?: string | null,
  ) =>
    agentName
      ? `${url}?agent_name=${agentName}&execution_scope=${executionScope}`
      : url,
}));

global.fetch = vi.fn();
global.window.open = vi.fn();

const drive = new GenericOAuthIntegrationProvider(
  "google_drive",
  "Google Drive",
);

describe("Generic OAuth integration provider", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it("lists the built-in provider cards in display order", () => {
    expect(
      builtInProviderCards.map(
        ({
          id,
          name,
          description,
          category,
          setup_type,
          icon,
          oauth_provider_id,
        }) => {
          if (!isValidElement<{ className: string }>(icon)) {
            throw new Error(`${id} has no icon element`);
          }
          return {
            id,
            name,
            description,
            category,
            setup_type,
            icon: icon.type,
            iconClass: icon.props.className,
            oauth_provider_id,
          };
        },
      ),
    ).toEqual([
      {
        id: "google_calendar",
        name: "Google Calendar",
        description:
          "View and schedule meetings with your connected Google Calendar",
        category: "productivity",
        setup_type: "oauth",
        icon: SiGooglecalendar,
        iconClass: "h-5 w-5 text-blue-600",
        oauth_provider_id: "google_calendar",
      },
      {
        id: "google_cloud",
        name: "Google Cloud",
        description:
          "Connect Google Cloud with read-only access for Google Cloud tools",
        category: "development",
        setup_type: "oauth",
        icon: SiGooglecloud,
        iconClass: "h-5 w-5 text-blue-600",
        oauth_provider_id: "google_cloud",
      },
      {
        id: "google_docs",
        name: "Google Docs",
        description: "Create, read, and edit documents with Google Docs",
        category: "productivity",
        setup_type: "oauth",
        icon: SiGoogledocs,
        iconClass: "h-5 w-5 text-blue-600",
        oauth_provider_id: "google_docs",
      },
      {
        id: "google_drive",
        name: "Google Drive",
        description:
          "Search, read, upload, and organize files in your connected Google Drive",
        category: "productivity",
        setup_type: "oauth",
        icon: SiGoogledrive,
        iconClass: "h-5 w-5 text-green-600",
        oauth_provider_id: "google_drive",
      },
      {
        id: "google_gmail",
        name: "Gmail",
        description: "Read, search, and manage Gmail emails",
        category: "email",
        setup_type: "oauth",
        icon: SiGmail,
        iconClass: "h-5 w-5 text-red-500",
        oauth_provider_id: "google_gmail",
      },
      {
        id: "google_sheets",
        name: "Google Sheets",
        description: "Read, create, and update Google Sheets spreadsheets",
        category: "development",
        setup_type: "oauth",
        icon: SiGooglesheets,
        iconClass: "h-5 w-5 text-green-600",
        oauth_provider_id: "google_sheets",
      },
      {
        id: "google_tasks",
        name: "Google Tasks",
        description:
          "List, create, update, complete, and delete tasks with Google Tasks",
        category: "productivity",
        setup_type: "oauth",
        icon: SiGoogletasks,
        iconClass: "h-5 w-5 text-blue-600",
        oauth_provider_id: "google_tasks",
      },
      {
        id: "homeassistant",
        name: "Home Assistant",
        description: "Control and monitor your smart home devices",
        category: "smart_home",
        setup_type: "special",
        icon: SiHomeassistant,
        iconClass: "h-5 w-5",
        oauth_provider_id: undefined,
      },
    ]);
  });

  it("loads status from the provider's own endpoint", async () => {
    const provider = new GenericOAuthIntegrationProvider(
      "google_cloud",
      "Google Cloud",
    );

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        connected: true,
        has_client_config: true,
        has_custom_client_config: false,
        has_service_account_config: false,
        client_config_service: "google_cloud_oauth_client",
      }),
    });

    const status = await provider.loadStatus();

    expect(global.fetch).toHaveBeenCalledWith("/api/oauth/google_cloud/status");
    expect(status).toMatchObject({
      status: "connected",
      oauth_client_config_service: "google_cloud_oauth_client",
    });
  });

  it("resolves connect once and releases observers after popup completion", async () => {
    vi.useFakeTimers();
    const removeListener = vi.spyOn(window, "removeEventListener");
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

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom-oauth",
          provider: "google_drive",
          status: "connected",
        },
        source: authWindow,
        origin: window.location.origin,
      }),
    );

    await connectPromise;
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom-oauth",
          provider: "google_drive",
          status: "connected",
        },
        source: authWindow,
        origin: window.location.origin,
      }),
    );

    expect(authWindow.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    expect(removeListener).toHaveBeenCalledWith(
      "message",
      expect.any(Function),
    );
    removeListener.mockRestore();
  });

  it("settles on the newer popup message too: connected resolves and cancelled rejects", async () => {
    const openPopup = () => {
      const authWindow = {
        closed: false,
        close: vi.fn(),
      } as unknown as Window;
      (global.window.open as any).mockReturnValue(authWindow);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
      });
      return authWindow;
    };
    const say = (authWindow: Window, status: string) =>
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "mindroom-oauth", provider: "google_drive", status },
          source: authWindow,
          origin: window.location.origin,
        }),
      );

    const connected = openPopup();
    const connect = drive.connect();
    await vi.waitFor(() => expect(global.window.open).toHaveBeenCalled());
    say(connected, "connected");
    await connect;
    expect(connected.close).toHaveBeenCalledOnce();

    const cancelled = openPopup();
    const cancel = drive.connect();
    const rejection = expect(cancel).rejects.toThrow(
      "Google Drive authorization was cancelled",
    );
    await vi.waitFor(() => expect(global.window.open).toHaveBeenCalledTimes(2));
    say(cancelled, "cancelled");
    await rejection;
    // The popup's page closes itself, so it is left alone.
    expect(cancelled.close).not.toHaveBeenCalled();
  });

  it("accepts OAuth completion from the backend origin returned by connect", async () => {
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
      json: async () => ({
        auth_url: "https://accounts.example.test/auth",
        completion_origin: "https://backend.example.test",
      }),
    });

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom-oauth",
          provider: "google_drive",
          status: "connected",
        },
        source: authWindow,
        origin: "https://backend.example.test",
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

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(global.window.open).toHaveBeenCalled();
    });
    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mindroom-oauth",
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

    const connectPromise = drive.connect();

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

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "not_connected",
      status_error: "server error",
      oauth_client_configured: false,
      oauth_custom_client_configured: false,
    });
  });

  it("fails closed when OAuth status cannot be loaded", async () => {
    (global.fetch as any).mockRejectedValueOnce(new Error("network error"));

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "not_connected",
      status_error: "Failed to load Google Drive OAuth status.",
      oauth_client_configured: false,
      oauth_custom_client_configured: false,
    });
  });

  it("maps OAuth client config service from provider status", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        connected: false,
        has_client_config: true,
        has_custom_client_config: false,
        has_service_account_config: false,
        client_config_service: "google_oauth_client",
      }),
    });

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "available",
      oauth_client_configured: true,
      oauth_custom_client_configured: false,
      oauth_client_config_service: "google_oauth_client",
    });
  });

  it("maps an unreadable OAuth status to a reset-required integration", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        connected: false,
        has_client_config: true,
        has_custom_client_config: false,
        has_service_account_config: false,
        client_config_service: "google_oauth_client",
        reset_required: true,
      }),
    });

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "not_connected",
      oauth_client_configured: true,
      oauth_reset_required: true,
    });
  });

  it("keeps service-account auth connected while offering unreadable OAuth reset", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        connected: true,
        has_client_config: false,
        has_custom_client_config: false,
        has_service_account_config: true,
        client_config_service: "google_oauth_client",
        reset_required: true,
      }),
    });

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "connected",
      oauth_service_account_configured: true,
      oauth_reset_required: true,
      helper_text:
        "Stored OAuth credentials cannot be read. Reset the OAuth connection to remove them.",
    });
  });

  it("reports why connect or disconnect failed", async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: false,
        json: async () => ({ detail: "Client is not allowed" }),
      })
      .mockResolvedValueOnce({ ok: false, json: async () => ({}) });

    await expect(drive.connect()).rejects.toThrow("Client is not allowed");
    await expect(drive.disconnect()).rejects.toThrow(
      "Failed to disconnect Google Drive",
    );
  });

  it("sends the selected agent scope with status, connect, and disconnect", async () => {
    const scope = { agentName: "mind", executionScope: "user" as const };
    (global.fetch as any)
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
      })
      .mockResolvedValueOnce({ ok: true });
    (global.window.open as any).mockReturnValue(null);

    await drive.loadStatus(scope);
    await expect(drive.connect(scope)).rejects.toThrow(
      "OAuth popup was blocked",
    );
    await drive.disconnect(scope);

    expect((global.fetch as any).mock.calls).toEqual([
      ["/api/oauth/google_drive/status?agent_name=mind&execution_scope=user"],
      [
        "/api/oauth/google_drive/connect?agent_name=mind&execution_scope=user",
        { method: "POST" },
      ],
      [
        "/api/oauth/google_drive/disconnect?agent_name=mind&execution_scope=user",
        { method: "POST" },
      ],
    ]);
  });
});
