/**
 * Central registry for all integrations
 */

import { createElement } from "react";
import { SiGoogledrive } from "react-icons/si";
import { API_BASE_URL, withAgentExecutionScope } from "@/lib/api";
import type { WorkerScope } from "@/types/config";
import {
  Integration,
  IntegrationConfig,
  IntegrationProvider,
  IntegrationScope,
} from "./types";
import { googleIntegration } from "./google";
import { spotifyIntegration } from "./spotify";
import { homeAssistantIntegration } from "./homeassistant";

class GenericOAuthIntegrationProvider implements IntegrationProvider {
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
    const connected = await this.checkConnection(
      scope?.agentName ?? null,
      scope?.executionScope,
    );
    return {
      status: connected ? "connected" : "available",
      connected,
    };
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
  ): Promise<boolean> {
    try {
      const response = await fetch(
        withAgentExecutionScope(
          `${API_BASE_URL}/api/oauth/${this.providerId}/status`,
          agentName,
          executionScope,
        ),
      );
      if (!response.ok) {
        return false;
      }
      const data = await response.json();
      return data.connected === true;
    } catch (error) {
      console.error(`Failed to load ${this.providerId} status:`, error);
      return false;
    }
  }

  private openAuthWindow(authUrl: string): Promise<void> {
    const authWindow = window.open(authUrl, "_blank", "width=500,height=700");
    if (!authWindow) {
      throw new Error("OAuth popup was blocked");
    }
    return new Promise((resolve) => {
      const pollInterval = window.setInterval(() => {
        if (authWindow.closed) {
          window.clearInterval(pollInterval);
          resolve();
        }
      }, 1000);
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

// Export all integration providers
export const integrationProviders: Record<string, IntegrationProvider> = {
  google: googleIntegration,
  google_drive: googleDriveIntegration,
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
