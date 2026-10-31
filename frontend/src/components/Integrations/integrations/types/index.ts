import type { WorkerScope } from "@/types/config";
import type { ToolFieldSchema } from "@/hooks/useTools";

/**
 * Core type definitions for all integrations
 */

export interface Integration {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: React.ReactNode;
  status: "connected" | "not_connected" | "available";
  setup_type: "oauth" | "api_key" | "special" | "none";
  docs_url?: string | null;
  helper_text?: string | null;
  config_fields?: ToolFieldSchema[] | null;
  icon_color?: string | null;
  auth_provider?: string;
  oauth_provider_id?: string;
  oauth_client_configured?: boolean;
  oauth_custom_client_configured?: boolean;
  oauth_client_config_service?: string;
  oauth_client_redirect_uri_supported?: boolean;
  oauth_service_account_configured?: boolean;
  oauth_reset_required?: boolean;
  status_error?: string;
  config_service?: string;
  oauth_fallback_fields?: string[];
  manual_auth_configured?: boolean;
  environment_auth_configured?: boolean;
}

/** A card before its provider reports a status. */
export type IntegrationCard = Omit<Integration, "status">;

/** What a provider reports about its connection, always including the card status. */
export type IntegrationStatus = Partial<Integration> &
  Pick<Integration, "status">;

export interface IntegrationScope {
  agentName?: string | null;
  executionScope?: WorkerScope | null;
}

export interface IntegrationConfig {
  /**
   * Handler for when the integration is selected/clicked
   */
  onAction?: () => Promise<void>;

  /**
   * Handler for disconnecting the integration
   */
  onDisconnect?: () => Promise<void>;

  /**
   * Custom component to render when configuring this integration
   */
  ConfigComponent?: React.ComponentType<{
    onClose: () => void;
    onSuccess?: () => void;
    agentName?: string | null;
    executionScope?: WorkerScope | null;
  }>;
}

export interface IntegrationProvider {
  /**
   * Get the configuration for this integration
   */
  getConfig(scope?: IntegrationScope): IntegrationConfig;

  /**
   * Load the current status of this integration
   */
  loadStatus(scope?: IntegrationScope): Promise<IntegrationStatus>;
}
