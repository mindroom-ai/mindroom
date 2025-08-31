import { FaSpotify } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';

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

  getConfig(): IntegrationConfig {
    return {
      integration: this.integration,
      onAction: this.connect.bind(this),
      onDisconnect: this.disconnect.bind(this),
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    const connected = await this.checkConnection();
    return {
      status: connected ? 'connected' : 'available',
      connected,
    };
  }

  private async connect(): Promise<void> {
    try {
      const response = await fetch(`${API_BASE}/api/integrations/spotify/connect`, {
        method: 'POST',
      });

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
          localStorage.setItem('spotify_configured', 'true');
          // The parent component should reload status after this
        }
      }, 2000);
    } catch (error) {
      console.error('Failed to connect Spotify:', error);
      throw error;
    }
  }

  private async disconnect(_integrationId: string): Promise<void> {
    localStorage.removeItem('spotify_configured');
    // Optionally call backend to revoke tokens
    try {
      await fetch(`${API_BASE}/api/integrations/spotify/disconnect`, {
        method: 'POST',
      });
    } catch (error) {
      console.error('Failed to disconnect Spotify:', error);
    }
  }

  private async checkConnection(): Promise<boolean> {
    // Check localStorage first for quick response
    const localConfig = localStorage.getItem('spotify_configured');
    if (localConfig) return true;

    // Then check backend for authoritative status
    try {
      const response = await fetch(`${API_BASE}/api/integrations/spotify/status`);
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
