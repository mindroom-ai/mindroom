/**
 * Central registry for all integrations
 */

import { createElement } from "react";
import {
  SiGmail,
  SiGooglecalendar,
  SiGoogledrive,
  SiGooglesheets,
} from "react-icons/si";
import { API_BASE_URL, withAgentExecutionScope } from "@/lib/api";
import type { WorkerScope } from "@/types/config";
import {
  Integration,
  IntegrationConfig,
  IntegrationProvider,
  IntegrationScope,
} from "./types";
import { spotifyIntegration } from "./spotify";
import { homeAssistantIntegration } from "./homeassistant";

const OAUTH_COMPLETE_MESSAGE_TYPE = "mindroom:oauth-complete";

type OAuthStatus = {
  connected: boolean;
  hasClientConfig: boolean;
  toolConfigService?: string;
  statusError?: string;
};

function isOAuthCompleteMessage(
  event: MessageEvent,
  authWindow: Window,
  providerId: string,
): boolean {
  if (
    event.source !== authWindow ||
    event.data === null ||
    typeof event.data !== "object"
  ) {
    return false;
  }
  const data = event.data as Record<string, unknown>;
  return (
    data.type === OAUTH_COMPLETE_MESSAGE_TYPE &&
    data.provider === providerId &&
    data.status === "connected"
  );
}

export class GenericOAuthIntegrationProvider implements IntegrationProvider {
  constructor(
    private readonly integration: Integration,
    private readonly providerId: string,
  ) {}

  getConfig(scope?: IntegrationScope): IntegrationConfig {
    const agentName = scope?.agentName ?? null;
    const executionScope = scope?.executionScope;
    return {
      integration: this.integration,
      onAction: () => this.connect(agentName, executionScope),
      onDisconnect: () => this.disconnect(agentName, executionScope),
    };
  }

  async loadStatus(scope?: IntegrationScope): Promise<Partial<Integration>> {
    const status = await this.checkConnection(
      scope?.agentName ?? null,
      scope?.executionScope,
    );
    const integrationStatus: Partial<Integration> = {
      status: status.connected
        ? "connected"
        : status.hasClientConfig
          ? "available"
          : "not_connected",
      connected: status.connected,
      oauth_client_configured: status.hasClientConfig,
      config_service: status.toolConfigService,
    };
    if (status.statusError) {
      integrationStatus.helper_text = status.statusError;
    }
    return integrationStatus;
  }

  private async connect(
    agentName?: string | null,
    executionScope?: WorkerScope | null,
  ): Promise<void> {
    const response = await fetch(
      withAgentExecutionScope(
        `${API_BASE_URL}/api/oauth/${this.providerId}/connect`,
        agentName,
        executionScope,
      ),
      { method: "POST" },
    );
    if (!response.ok) {
      const error = await response.json();
      throw new Error(
        error.detail || `Failed to connect ${this.integration.name}`,
      );
    }
    const data = await response.json();
    if (typeof data.auth_url !== "string" || data.auth_url.length === 0) {
      throw new Error(`Failed to connect ${this.integration.name}`);
    }
    await this.openAuthWindow(data.auth_url);
  }

  private async disconnect(
    agentName?: string | null,
    executionScope?: WorkerScope | null,
  ): Promise<void> {
    const response = await fetch(
      withAgentExecutionScope(
        `${API_BASE_URL}/api/oauth/${this.providerId}/disconnect`,
        agentName,
        executionScope,
      ),
      { method: "POST" },
    );
    if (!response.ok) {
      const error = await response.json();
      throw new Error(
        error.detail || `Failed to disconnect ${this.integration.name}`,
      );
    }
  }

  private async checkConnection(
    agentName?: string | null,
    executionScope?: WorkerScope | null,
  ): Promise<OAuthStatus> {
    try {
      const response = await fetch(
        withAgentExecutionScope(
          `${API_BASE_URL}/api/oauth/${this.providerId}/status`,
          agentName,
          executionScope,
        ),
      );
      if (!response.ok) {
        let detail = `Failed to load ${this.integration.name} OAuth status.`;
        try {
          const data = await response.json();
          if (typeof data.detail === "string" && data.detail.length > 0) {
            detail = data.detail;
          }
        } catch {
          // Keep the generic status error when the response body is not JSON.
        }
        return {
          connected: false,
          hasClientConfig: true,
          statusError: detail,
        };
      }
      const data = await response.json();
      return {
        connected: data.connected === true,
        hasClientConfig: data.has_client_config === true,
        toolConfigService:
          typeof data.tool_config_service === "string"
            ? data.tool_config_service
            : undefined,
      };
    } catch (error) {
      console.error(`Failed to load ${this.providerId} status:`, error);
      return {
        connected: false,
        hasClientConfig: true,
        statusError: `Failed to load ${this.integration.name} OAuth status.`,
      };
    }
  }

  private openAuthWindow(authUrl: string): Promise<void> {
    const authWindow = window.open(authUrl, "_blank", "width=500,height=700");
    if (!authWindow) {
      throw new Error("OAuth popup was blocked");
    }
    return new Promise((resolve, reject) => {
      let completed = false;
      let receivedCompletion = false;
      let pollInterval = 0;
      const finish = (error?: Error) => {
        if (completed) {
          return;
        }
        completed = true;
        window.clearInterval(pollInterval);
        window.removeEventListener("message", handleMessage);
        if (error) {
          reject(error);
          return;
        }
        resolve();
      };
      const handleMessage = (event: MessageEvent) => {
        if (!isOAuthCompleteMessage(event, authWindow, this.providerId)) {
          return;
        }
        receivedCompletion = true;
        if (!authWindow.closed) {
          authWindow.close();
        }
        finish();
      };
      pollInterval = window.setInterval(() => {
        if (authWindow.closed) {
          finish(
            receivedCompletion
              ? undefined
              : new Error(
                  `${this.integration.name} authorization was cancelled`,
                ),
          );
        }
      }, 1000);
      window.addEventListener("message", handleMessage);
    });
  }
}

const googleDriveIntegration = new GenericOAuthIntegrationProvider(
  {
    id: "google_drive",
    name: "Google Drive",
    description: "Search and read files from your connected Google Drive",
    category: "productivity",
    icon: createElement(SiGoogledrive, {
      className: "h-5 w-5 text-green-600",
    }),
    status: "available",
    setup_type: "oauth",
    connected: false,
  },
  "google_drive",
);

const googleCalendarIntegration = new GenericOAuthIntegrationProvider(
  {
    id: "google_calendar",
    name: "Google Calendar",
    description:
      "View and schedule meetings with your connected Google Calendar",
    category: "productivity",
    icon: createElement(SiGooglecalendar, {
      className: "h-5 w-5 text-blue-600",
    }),
    status: "available",
    setup_type: "oauth",
    connected: false,
  },
  "google_calendar",
);

const googleSheetsIntegration = new GenericOAuthIntegrationProvider(
  {
    id: "google_sheets",
    name: "Google Sheets",
    description:
      "Read, create, update, and duplicate Google Sheets spreadsheets",
    category: "development",
    icon: createElement(SiGooglesheets, {
      className: "h-5 w-5 text-green-600",
    }),
    status: "available",
    setup_type: "oauth",
    connected: false,
  },
  "google_sheets",
);

const googleGmailIntegration = new GenericOAuthIntegrationProvider(
  {
    id: "google_gmail",
    name: "Gmail",
    description: "Read, search, and manage Gmail emails",
    category: "email",
    icon: createElement(SiGmail, {
      className: "h-5 w-5 text-red-500",
    }),
    status: "available",
    setup_type: "oauth",
    connected: false,
  },
  "google_gmail",
);

// Export all integration providers
export const integrationProviders: Record<string, IntegrationProvider> = {
  google_calendar: googleCalendarIntegration,
  google_drive: googleDriveIntegration,
  google_gmail: googleGmailIntegration,
  google_sheets: googleSheetsIntegration,
  spotify: spotifyIntegration,
  homeassistant: homeAssistantIntegration,
};

// Export types
export type {
  Integration,
  IntegrationConfig,
  IntegrationProvider,
  IntegrationScope,
} from "./types";

// Helper function to get all integrations
export function getAllIntegrations(): IntegrationProvider[] {
  return Object.values(integrationProviders);
}

// Helper function to get integration by ID
export function getIntegrationById(
  id: string,
): IntegrationProvider | undefined {
  return integrationProviders[id];
}
