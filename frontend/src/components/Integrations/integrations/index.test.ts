import { createElement } from "react";
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
import { builtInProviderCards, oauthIntegrationProvider } from "./index";

const fetchMock = vi.fn();
global.fetch = fetchMock as typeof fetch;
const openMock = vi.fn();
window.open = openMock as typeof window.open;

const drive = oauthIntegrationProvider("google_drive", "Google Drive");

function openPopup(
  connectBody: Record<string, unknown> = {
    auth_url: "https://accounts.example.test/auth",
  },
) {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => connectBody });
  const popup = {
    closed: false,
    close: vi.fn(() => {
      popup.closed = true;
    }),
  };
  openMock.mockReturnValue(popup);
  return popup;
}

function postOutcome(
  popup: object,
  status: string,
  origin = window.location.origin,
) {
  window.dispatchEvent(
    new MessageEvent("message", {
      data: { type: "mindroom-oauth", provider: "google_drive", status },
      source: popup as Window,
      origin,
    }),
  );
}

describe("Generic OAuth integration provider", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    fetchMock.mockReset();
  });

  it("lists the built-in provider cards in display order", () => {
    expect(builtInProviderCards).toEqual([
      {
        id: "google_calendar",
        name: "Google Calendar",
        description:
          "View and schedule meetings with your connected Google Calendar",
        category: "productivity",
        setup_type: "oauth",
        icon: createElement(SiGooglecalendar, {
          className: "h-5 w-5 text-blue-600",
        }),
        oauth_provider_id: "google_calendar",
      },
      {
        id: "google_cloud",
        name: "Google Cloud",
        description:
          "Connect Google Cloud with read-only access for Google Cloud tools",
        category: "development",
        setup_type: "oauth",
        icon: createElement(SiGooglecloud, {
          className: "h-5 w-5 text-blue-600",
        }),
        oauth_provider_id: "google_cloud",
      },
      {
        id: "google_docs",
        name: "Google Docs",
        description: "Create, read, and edit documents with Google Docs",
        category: "productivity",
        setup_type: "oauth",
        icon: createElement(SiGoogledocs, {
          className: "h-5 w-5 text-blue-600",
        }),
        oauth_provider_id: "google_docs",
      },
      {
        id: "google_drive",
        name: "Google Drive",
        description:
          "Search, read, upload, and organize files in your connected Google Drive",
        category: "productivity",
        setup_type: "oauth",
        icon: createElement(SiGoogledrive, {
          className: "h-5 w-5 text-green-600",
        }),
        oauth_provider_id: "google_drive",
      },
      {
        id: "google_gmail",
        name: "Gmail",
        description: "Read, search, and manage Gmail emails",
        category: "email",
        setup_type: "oauth",
        icon: createElement(SiGmail, { className: "h-5 w-5 text-red-500" }),
        oauth_provider_id: "google_gmail",
      },
      {
        id: "google_sheets",
        name: "Google Sheets",
        description: "Read, create, and update Google Sheets spreadsheets",
        category: "development",
        setup_type: "oauth",
        icon: createElement(SiGooglesheets, {
          className: "h-5 w-5 text-green-600",
        }),
        oauth_provider_id: "google_sheets",
      },
      {
        id: "google_tasks",
        name: "Google Tasks",
        description:
          "List, create, update, complete, and delete tasks with Google Tasks",
        category: "productivity",
        setup_type: "oauth",
        icon: createElement(SiGoogletasks, {
          className: "h-5 w-5 text-blue-600",
        }),
        oauth_provider_id: "google_tasks",
      },
      {
        id: "homeassistant",
        name: "Home Assistant",
        description: "Control and monitor your smart home devices",
        category: "smart_home",
        setup_type: "special",
        icon: createElement(SiHomeassistant, { className: "h-5 w-5" }),
      },
    ]);
  });

  it("resolves connect once and releases observers after popup completion", async () => {
    vi.useFakeTimers();
    const removeListener = vi.spyOn(window, "removeEventListener");
    const popup = openPopup();

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(openMock).toHaveBeenCalled();
    });
    postOutcome(popup, "connected");

    await connectPromise;
    postOutcome(popup, "connected");

    expect(popup.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    expect(removeListener).toHaveBeenCalledWith(
      "message",
      expect.any(Function),
    );
    removeListener.mockRestore();
  });

  it("rejects a cancelled popup and leaves it to close itself", async () => {
    const cancelled = openPopup();
    const cancel = drive.connect();
    const rejection = expect(cancel).rejects.toThrow(
      "Google Drive authorization was cancelled",
    );
    await vi.waitFor(() => expect(openMock).toHaveBeenCalled());
    postOutcome(cancelled, "cancelled");
    await rejection;
    // The popup's page closes itself, so it is left alone.
    expect(cancelled.close).not.toHaveBeenCalled();
  });

  it("accepts OAuth completion from the backend origin returned by connect", async () => {
    const popup = openPopup({
      auth_url: "https://accounts.example.test/auth",
      completion_origin: "https://backend.example.test",
    });

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(openMock).toHaveBeenCalled();
    });
    postOutcome(popup, "connected", "https://backend.example.test");

    await connectPromise;

    expect(popup.close).toHaveBeenCalled();
  });

  it("ignores OAuth completion messages from other origins", async () => {
    vi.useFakeTimers();
    const popup = openPopup();

    const connectPromise = drive.connect();

    await vi.waitFor(() => {
      expect(openMock).toHaveBeenCalled();
    });
    postOutcome(popup, "connected", "https://evil.example.test");

    const rejection = expect(connectPromise).rejects.toThrow(
      "Google Drive authorization was cancelled",
    );
    popup.closed = true;
    await vi.advanceTimersByTimeAsync(1000);

    await rejection;
    expect(popup.close).not.toHaveBeenCalled();
  });

  it("fails closed when OAuth status fails", async () => {
    fetchMock.mockResolvedValueOnce({
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
    fetchMock.mockRejectedValueOnce(new Error("network error"));

    const status = await drive.loadStatus();

    expect(status).toMatchObject({
      status: "not_connected",
      status_error: "Failed to load Google Drive OAuth status.",
      oauth_client_configured: false,
      oauth_custom_client_configured: false,
    });
  });

  it("maps OAuth client config service from provider status", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        connected: false,
        has_client_config: true,
        has_custom_client_config: true,
        has_service_account_config: false,
        client_config_service: "google_oauth_client",
        client_config_redirect_uri_supported: true,
        default_redirect_uri:
          "https://mindroom.example.org/api/oauth/google_drive/callback",
      }),
    });

    const status = await drive.loadStatus();

    expect(fetchMock).toHaveBeenCalledWith("/api/oauth/google_drive/status");
    expect(status).toMatchObject({
      status: "available",
      oauth_client_configured: true,
      oauth_custom_client_configured: true,
      oauth_client_config_service: "google_oauth_client",
      oauth_client_redirect_uri_supported: true,
      oauth_default_redirect_uri:
        "https://mindroom.example.org/api/oauth/google_drive/callback",
    });
  });

  it("maps an unreadable OAuth status to a reset-required integration", async () => {
    fetchMock.mockResolvedValueOnce({
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
    fetchMock.mockResolvedValueOnce({
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
    fetchMock
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

  it("falls back to a generic connect error for a non-JSON error body", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    });

    await expect(drive.connect()).rejects.toThrow(
      "Failed to connect Google Drive",
    );
  });

  it("fails connect when the backend returns no authorization URL", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ auth_url: "" }),
    });

    await expect(drive.connect()).rejects.toThrow(
      "Failed to connect Google Drive",
    );
    expect(openMock).not.toHaveBeenCalled();
  });

  it("sends the selected agent scope with status, connect, and disconnect", async () => {
    const scope = { agentName: "mind", executionScope: "user" as const };
    fetchMock
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ auth_url: "https://accounts.example.test/auth" }),
      })
      .mockResolvedValueOnce({ ok: true });
    openMock.mockReturnValue(null);

    await drive.loadStatus(scope);
    await expect(drive.connect(scope)).rejects.toThrow(
      "OAuth popup was blocked",
    );
    await drive.disconnect(scope);

    expect(fetchMock.mock.calls).toEqual([
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
