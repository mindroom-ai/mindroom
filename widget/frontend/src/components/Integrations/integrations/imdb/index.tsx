import { FaImdb } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';
import { IMDbConfigDialog } from './IMDbConfigDialog';

class IMDbIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'imdb',
    name: 'IMDb',
    description: 'Movie and TV show database',
    category: 'entertainment',
    icon: <FaImdb className="h-5 w-5" />,
    status: 'available',
    setup_type: 'api_key',
    connected: false,
  };

  getConfig(): IntegrationConfig {
    return {
      integration: this.integration,
      onAction: async () => {
        // Parent component will handle showing the config dialog
      },
      onDisconnect: this.disconnect.bind(this),
      ConfigComponent: IMDbConfigDialog,
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

  private async disconnect(_integrationId: string): Promise<void> {
    localStorage.removeItem('imdb_configured');
    // Optionally call backend to remove stored credentials
    try {
      await fetch(`${API_BASE}/api/integrations/imdb/disconnect`, {
        method: 'POST',
      });
    } catch (error) {
      console.error('Failed to disconnect IMDb:', error);
    }
  }

  private async checkConnection(): Promise<boolean> {
    // Check localStorage first
    const localConfig = localStorage.getItem('imdb_configured');
    if (localConfig) return true;

    // Then check backend
    try {
      const response = await fetch(`${API_BASE}/api/integrations/imdb/status`);
      if (response.ok) {
        const data = await response.json();
        return data.connected === true;
      }
    } catch (error) {
      console.error('Failed to check IMDb connection:', error);
    }
    return false;
  }
}

export const imdbIntegration = new IMDbIntegrationProvider();
