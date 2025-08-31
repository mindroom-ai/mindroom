// Spotify icon inline to avoid react-icons import
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';

class SpotifyIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'spotify',
    name: 'Spotify',
    description: 'Music streaming service integration',
    category: 'entertainment',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="h-5 w-5">
        <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z" />
      </svg>
    ),
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
