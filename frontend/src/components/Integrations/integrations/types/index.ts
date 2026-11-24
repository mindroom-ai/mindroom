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
  auth_provider?: string;
  oauth_provider_id?: string;
  oauth_client_configured?: boolean;
  oauth_custom_client_configured?: boolean;
  oauth_client_config_service?: string;
  oauth_client_redirect_uri_supported?: boolean;
  oauth_default_redirect_uri?: string;
  oauth_service_account_configured?: boolean;
  oauth_reset_required?: boolean;
  status_error?: string;
  config_service?: string;
  oauth_fallback_fields?: string[];
  manual_auth_configured?: boolean;
  environment_auth_configured?: boolean;
}

/** A provider's card before the provider reports its status. */
export type ProviderCard = Omit<Integration, "status">;

/** What a provider reports about its connection, always including the card status. */
export type IntegrationStatus = Partial<Integration> &
  Pick<Integration, "status">;

export interface IntegrationScope {
  agentName?: string | null;
  executionScope?: WorkerScope | null;
}

export interface IntegrationProvider {
  loadStatus(scope?: IntegrationScope): Promise<IntegrationStatus>;

  /** Start the provider's own connection flow; providers set up in a dialog have none. */
  connect?(scope?: IntegrationScope): Promise<void>;

  disconnect(scope?: IntegrationScope): Promise<void>;

  /** Dialog content that sets up this integration. */
  SetupComponent?: React.ComponentType<
    IntegrationScope & { onSuccess: () => void }
  >;
}
