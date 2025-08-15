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
  status: 'connected' | 'not_connected' | 'available' | 'coming_soon';
  setup_type: 'oauth' | 'api_key' | 'special' | 'coming_soon' | 'none';
  connected?: boolean;
  details?: any;
  docs_url?: string | null;
  helper_text?: string | null;
}

export interface IntegrationConfig {
  /**
   * The integration definition
   */
  integration: Integration;

  /**
   * Handler for when the integration is selected/clicked
   */
  onAction: (integration: Integration) => void | Promise<void>;

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
  }>;

  /**
   * Check if the integration is connected
   */
  checkConnection?: () => Promise<boolean>;

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
  getConfig(): IntegrationConfig;

  /**
   * Load the current status of this integration
   */
  loadStatus?: () => Promise<Partial<Integration>>;
}
