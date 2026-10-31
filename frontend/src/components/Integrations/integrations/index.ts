/**
 * Built-in integration cards and the generic OAuth provider behind every OAuth card
 */

import { createElement } from "react";
import type { IconType } from "react-icons";
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
import { API_BASE_URL, withAgentExecutionScope } from "@/lib/api";
import { watchOAuthCompletion } from "@/lib/oauthCompletion";
import {
  IntegrationProvider,
  IntegrationScope,
  IntegrationStatus,
  ProviderCard,
} from "./types";

type OAuthStatus = {
  connected: boolean;
  hasClientConfig: boolean;
  hasCustomClientConfig: boolean;
  hasServiceAccountConfig: boolean;
  clientConfigService?: string;
  clientConfigRedirectUriSupported: boolean;
  resetRequired: boolean;
  statusError?: string;
};

const unavailableStatus = (statusError: string): OAuthStatus => ({
  connected: false,
  hasClientConfig: false,
  hasCustomClientConfig: false,
  hasServiceAccountConfig: false,
  clientConfigRedirectUriSupported: false,
  resetRequired: false,
  statusError,
});

function oauthCompletionOrigin(rawOrigin: unknown): string {
  if (typeof rawOrigin === "string" && rawOrigin.length > 0) {
    return new URL(rawOrigin).origin;
  }
  if (API_BASE_URL && /^https?:\/\//.test(API_BASE_URL)) {
    return new URL(API_BASE_URL).origin;
  }
  return window.location.origin;
}

export class GenericOAuthIntegrationProvider implements IntegrationProvider {
  constructor(
    private readonly providerId: string,
    private readonly displayName: string,
  ) {}

  private url(
    action: "connect" | "disconnect" | "status",
    scope?: IntegrationScope,
  ): string {
    return withAgentExecutionScope(
      `${API_BASE_URL}/api/oauth/${this.providerId}/${action}`,
      scope?.agentName,
      scope?.executionScope,
    );
  }

  async loadStatus(scope?: IntegrationScope): Promise<IntegrationStatus> {
    const status = await this.checkConnection(scope);
    const integrationStatus: IntegrationStatus = {
      status: status.connected
        ? "connected"
        : !status.resetRequired && status.hasClientConfig
          ? "available"
          : "not_connected",
      oauth_client_configured: status.hasClientConfig,
      oauth_custom_client_configured: status.hasCustomClientConfig,
      oauth_client_config_service: status.clientConfigService,
      oauth_client_redirect_uri_supported:
        status.clientConfigRedirectUriSupported,
      oauth_service_account_configured: status.hasServiceAccountConfig,
      oauth_reset_required: status.resetRequired,
    };
    if (status.resetRequired) {
      integrationStatus.helper_text =
        "Stored OAuth credentials cannot be read. Reset the OAuth connection to remove them.";
    }
    if (status.statusError) {
      integrationStatus.helper_text = status.statusError;
      integrationStatus.status_error = status.statusError;
    }
    return integrationStatus;
  }

  async connect(scope?: IntegrationScope): Promise<void> {
    const data = await (await this.post("connect", scope)).json();
    if (typeof data.auth_url !== "string" || data.auth_url.length === 0) {
      throw new Error(`Failed to connect ${this.displayName}`);
    }
    await this.openAuthWindow(
      data.auth_url,
      oauthCompletionOrigin(data.completion_origin),
    );
  }

  async disconnect(scope?: IntegrationScope): Promise<void> {
    await this.post("disconnect", scope);
  }

  private async post(
    action: "connect" | "disconnect",
    scope?: IntegrationScope,
  ): Promise<Response> {
    const response = await fetch(this.url(action, scope), { method: "POST" });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(
        error.detail || `Failed to ${action} ${this.displayName}`,
      );
    }
    return response;
  }

  private async checkConnection(
    scope?: IntegrationScope,
  ): Promise<OAuthStatus> {
    try {
      const response = await fetch(this.url("status", scope));
      if (!response.ok) {
        let detail = `Failed to load ${this.displayName} OAuth status.`;
        try {
          const data = await response.json();
          if (typeof data.detail === "string" && data.detail.length > 0) {
            detail = data.detail;
          }
        } catch {
          // Keep the generic status error when the response body is not JSON.
        }
        return unavailableStatus(detail);
      }
      const data = await response.json();
      return {
        connected: data.connected === true,
        hasClientConfig: data.has_client_config === true,
        hasCustomClientConfig: data.has_custom_client_config === true,
        hasServiceAccountConfig: data.has_service_account_config === true,
        resetRequired: data.reset_required === true,
        clientConfigRedirectUriSupported:
          data.client_config_redirect_uri_supported === true,
        clientConfigService:
          typeof data.client_config_service === "string"
            ? data.client_config_service
            : undefined,
      };
    } catch (error) {
      console.error(`Failed to load ${this.providerId} status:`, error);
      return unavailableStatus(
        `Failed to load ${this.displayName} OAuth status.`,
      );
    }
  }

  private openAuthWindow(
    authUrl: string,
    expectedCompletionOrigin: string,
  ): Promise<void> {
    const authWindow = window.open(authUrl, "_blank", "width=500,height=700");
    if (!authWindow) {
      throw new Error("OAuth popup was blocked");
    }
    return new Promise((resolve, reject) => {
      watchOAuthCompletion(authWindow, {
        provider: this.providerId,
        expectedOrigin: () => expectedCompletionOrigin,
        pollIntervalMs: 1000,
        cancellationMessage: `${this.displayName} authorization was cancelled`,
        onSettled: (error) => {
          if (error) {
            reject(error);
            return;
          }
          if (!authWindow.closed) authWindow.close();
          resolve();
        },
      });
    });
  }
}

const googleCard = (
  id: string,
  name: string,
  description: string,
  category: string,
  icon: IconType,
  iconColor: string,
): ProviderCard => ({
  id,
  name,
  description,
  category,
  icon: createElement(icon, { className: `h-5 w-5 ${iconColor}` }),
  setup_type: "oauth",
  oauth_provider_id: id,
});

// Built-in cards show whether or not the tools list names their provider, so also before it loads.
// Google Cloud's card is named after the connection, not after its BigQuery tool.
export const builtInProviderCards: ProviderCard[] = [
  googleCard(
    "google_calendar",
    "Google Calendar",
    "View and schedule meetings with your connected Google Calendar",
    "productivity",
    SiGooglecalendar,
    "text-blue-600",
  ),
  googleCard(
    "google_cloud",
    "Google Cloud",
    "Connect Google Cloud with read-only access for Google Cloud tools",
    "development",
    SiGooglecloud,
    "text-blue-600",
  ),
  googleCard(
    "google_docs",
    "Google Docs",
    "Create, read, and edit documents with Google Docs",
    "productivity",
    SiGoogledocs,
    "text-blue-600",
  ),
  googleCard(
    "google_drive",
    "Google Drive",
    "Search, read, upload, and organize files in your connected Google Drive",
    "productivity",
    SiGoogledrive,
    "text-green-600",
  ),
  googleCard(
    "google_gmail",
    "Gmail",
    "Read, search, and manage Gmail emails",
    "email",
    SiGmail,
    "text-red-500",
  ),
  googleCard(
    "google_sheets",
    "Google Sheets",
    "Read, create, and update Google Sheets spreadsheets",
    "development",
    SiGooglesheets,
    "text-green-600",
  ),
  googleCard(
    "google_tasks",
    "Google Tasks",
    "List, create, update, complete, and delete tasks with Google Tasks",
    "productivity",
    SiGoogletasks,
    "text-blue-600",
  ),
  {
    id: "homeassistant",
    name: "Home Assistant",
    description: "Control and monitor your smart home devices",
    category: "smart_home",
    icon: createElement(SiHomeassistant, { className: "h-5 w-5" }),
    setup_type: "special",
  },
];

export type {
  Integration,
  IntegrationProvider,
  IntegrationStatus,
  ProviderCard,
} from "./types";
