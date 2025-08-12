/**
 * Central registry for all integrations
 */

import { IntegrationProvider } from './types';
import { googleIntegration } from './google';
import { spotifyIntegration } from './spotify';
import { homeAssistantIntegration } from './homeassistant';

// Export all integration providers
export const integrationProviders: Record<string, IntegrationProvider> = {
  google: googleIntegration,
  spotify: spotifyIntegration,
  homeassistant: homeAssistantIntegration,
};

// Export types
export type { Integration, IntegrationConfig, IntegrationProvider } from './types';

// Helper function to get all integrations
export function getAllIntegrations(): IntegrationProvider[] {
  return Object.values(integrationProviders);
}

// Helper function to get integration by ID
export function getIntegrationById(id: string): IntegrationProvider | undefined {
  return integrationProviders[id];
}
