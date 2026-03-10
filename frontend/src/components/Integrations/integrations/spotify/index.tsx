import { FaSpotify } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig, IntegrationScope } from '../types';
import { API_BASE_URL, withAgentName } from '@/lib/api';

class SpotifyIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'spotify',
    name: 'Spotify',
    description: 'Music streaming service integration',
    category: 'entertainment',
    icon: <FaSpotify className="h-5 w-5" />,
    status: 'available',
    setup_type: 'oauth',
    connected: false,
  };

  private localStorageKey(agentName?: string | null): string {
    return agentName ? `spotify_configured:${agentName}` : 'spotify_configured';
  }

  getConfig(scope?: IntegrationScope): IntegrationConfig {
    const agentName = scope?.agentName ?? null;
    return {
      integration: this.integration,
      onAction: () => this.connect(agentName),
      onDisconnect: () => this.disconnect(agentName),
    };
  }

  async loadStatus(scope?: IntegrationScope): Promise<Partial<Integration>> {
    const connected = await this.checkConnection(scope?.agentName ?? null);
    return {
      status: connected ? 'connected' : 'available',
      connected,
    };
  }

  private async connect(agentName?: string | null): Promise<void> {
    try {
      const response = await fetch(
        withAgentName(`${API_BASE_URL}/api/integrations/spotify/connect`, agentName),
        {
          method: 'POST',
        }
      );

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to connect Spotify');
      }

      const data = await response.json();
      const authWindow = window.open(data.auth_url, '_blank', 'width=500,height=600');

      // Poll for window closure
      const pollInterval = setInterval(async () => {
        if (authWindow?.closed) {
          clearInterval(pollInterval);
          localStorage.setItem(this.localStorageKey(agentName), 'true');
          // The parent component should reload status after this
        }
      }, 2000);
    } catch (error) {
      console.error('Failed to connect Spotify:', error);
      throw error;
    }
  }

  private async disconnect(agentName?: string | null): Promise<void> {
    localStorage.removeItem(this.localStorageKey(agentName));
    // Optionally call backend to revoke tokens
    try {
      await fetch(withAgentName(`${API_BASE_URL}/api/integrations/spotify/disconnect`, agentName), {
        method: 'POST',
      });
    } catch (error) {
      console.error('Failed to disconnect Spotify:', error);
    }
  }

  private async checkConnection(agentName?: string | null): Promise<boolean> {
    // Check localStorage first for quick response
    const localConfig = localStorage.getItem(this.localStorageKey(agentName));
    if (localConfig) return true;

    // Then check backend for authoritative status
    try {
      const response = await fetch(
        withAgentName(`${API_BASE_URL}/api/integrations/spotify/status`, agentName)
      );
      if (response.ok) {
        const data = await response.json();
        return data.connected === true;
      }
    } catch (error) {
      console.error('Failed to check Spotify connection:', error);
    }
    return false;
  }
}

export const spotifyIntegration = new SpotifyIntegrationProvider();
