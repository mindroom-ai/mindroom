import type { WorkerScope } from '@/types/config';

/**
 * Core type definitions for all integrations
 */

export interface Integration {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: React.ReactNode;
  iconColor?: string | null;
  status: 'connected' | 'not_connected' | 'available';
  setup_type: 'oauth' | 'api_key' | 'special' | 'none';
  connected?: boolean;
  details?: any;
  docs_url?: string | null;
  helper_text?: string | null;
}

export interface IntegrationScope {
  agentName?: string | null;
  executionScope?: WorkerScope | null;
}

export interface IntegrationConfig {
  /**
   * The integration definition
   */
  integration: Integration;

  /**
   * Handler for when the integration is selected/clicked
   */
  onAction?: (integration: Integration) => void | Promise<void>;

  /**
   * Handler for disconnecting the integration
   */
  onDisconnect?: (integrationId: string) => void | Promise<void>;

  /**
   * Custom component to render when configuring this integration
   */
  ConfigComponent?: React.ComponentType<{
    onClose: () => void;
    onSuccess?: () => void;
    agentName?: string | null;
    executionScope?: WorkerScope | null;
  }>;

  /**
   * Custom action button component
   */
  ActionButton?: React.ComponentType<{
    integration: Integration;
    loading: boolean;
    onAction: () => void;
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
  loadStatus?: (scope?: IntegrationScope) => Promise<Partial<Integration>>;
}
