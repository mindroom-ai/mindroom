/**
 * Built-in integration cards and the generic OAuth provider behind every OAuth card
 */

import { createElement, type ReactNode } from "react";
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
import {
  apiErrorMessageFromResponse,
  withAgentExecutionScope,
} from "@/lib/api";
import { watchOAuthCompletion } from "@/lib/oauthCompletion";
import {
  IntegrationProvider,
  IntegrationScope,
  IntegrationStatus,
  ProviderCard,
} from "./types";

export const scopedApiUrl = (path: string, scope?: IntegrationScope) =>
  withAgentExecutionScope(path, scope?.agentName, scope?.executionScope);

function oauthCompletionOrigin(rawOrigin: unknown): string {
  if (typeof rawOrigin === "string" && rawOrigin.length > 0) {
    return new URL(rawOrigin).origin;
  }
  return window.location.origin;
}

const unavailableStatus = (statusError: string): IntegrationStatus => ({
  status: "not_connected",
  oauth_client_configured: false,
  oauth_custom_client_configured: false,
  oauth_client_redirect_uri_supported: false,
  oauth_service_account_configured: false,
  oauth_reset_required: false,
  helper_text: statusError,
  status_error: statusError,
});

/** The provider behind every OAuth card, served by `/api/oauth/<providerId>/…`. */
export function oauthIntegrationProvider(
  providerId: string,
  displayName: string,
) {
  const url = (
    action: "connect" | "disconnect" | "status",
    scope?: IntegrationScope,
  ) => scopedApiUrl(`/api/oauth/${providerId}/${action}`, scope);

  const post = async (
    action: "connect" | "disconnect",
    scope?: IntegrationScope,
  ) => {
    const response = await fetch(url(action, scope), { method: "POST" });
    if (!response.ok) {
      throw new Error(
        await apiErrorMessageFromResponse(
          response,
          `Failed to ${action} ${displayName}`,
        ),
      );
    }
    return response;
  };

  const openAuthWindow = (
    authUrl: string,
    expectedCompletionOrigin: string,
  ): Promise<void> => {
    const authWindow = window.open(authUrl, "_blank", "width=500,height=700");
    if (!authWindow) {
      throw new Error("OAuth popup was blocked");
    }
    return new Promise((resolve, reject) => {
      watchOAuthCompletion(authWindow, {
        provider: providerId,
        expectedOrigin: () => expectedCompletionOrigin,
        pollIntervalMs: 1000,
        cancellationMessage: `${displayName} authorization was cancelled`,
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
  };

  return {
    async loadStatus(scope?: IntegrationScope) {
      const fallback = `Failed to load ${displayName} OAuth status.`;
      try {
        const response = await fetch(url("status", scope));
        if (!response.ok) {
          return unavailableStatus(
            await apiErrorMessageFromResponse(response, fallback),
          );
        }
        const data = await response.json();
        const hasClientConfig = data.has_client_config === true;
        const resetRequired = data.reset_required === true;
        const status: IntegrationStatus = {
          status:
            data.connected === true
              ? "connected"
              : !resetRequired && hasClientConfig
                ? "available"
                : "not_connected",
          oauth_client_configured: hasClientConfig,
          oauth_custom_client_configured:
            data.has_custom_client_config === true,
          oauth_client_config_service:
            typeof data.client_config_service === "string"
              ? data.client_config_service
              : undefined,
          oauth_client_redirect_uri_supported:
            data.client_config_redirect_uri_supported === true,
          oauth_default_redirect_uri:
            typeof data.default_redirect_uri === "string"
              ? data.default_redirect_uri
              : undefined,
          oauth_service_account_configured:
            data.has_service_account_config === true,
          oauth_reset_required: resetRequired,
        };
        if (resetRequired) {
          status.helper_text =
            "Stored OAuth credentials cannot be read. Reset the OAuth connection to remove them.";
        }
        return status;
      } catch (error) {
        console.error(`Failed to load ${providerId} status:`, error);
        return unavailableStatus(fallback);
      }
    },

    async connect(scope?: IntegrationScope) {
      const data = await (await post("connect", scope)).json();
      if (typeof data.auth_url !== "string" || data.auth_url.length === 0) {
        throw new Error(`Failed to connect ${displayName}`);
      }
      await openAuthWindow(
        data.auth_url,
        oauthCompletionOrigin(data.completion_origin),
      );
    },

    async disconnect(scope?: IntegrationScope) {
      await post("disconnect", scope);
    },
  } satisfies IntegrationProvider;
}

/** A provider card whose OAuth provider shares its ID. */
export const oauthCard = (
  id: string,
  name: string,
  description: string,
  category: string,
  icon: ReactNode,
): ProviderCard => ({
  id,
  name,
  description,
  category,
  icon,
  setup_type: "oauth",
  oauth_provider_id: id,
});

const googleCard = (
  id: string,
  name: string,
  description: string,
  category: string,
  icon: IconType,
  iconColor: string,
) =>
  oauthCard(
    id,
    name,
    description,
    category,
    createElement(icon, { className: `h-5 w-5 ${iconColor}` }),
  );

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
