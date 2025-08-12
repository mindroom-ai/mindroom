import { FaImdb } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';
import { IMDbConfigWrapper } from './IMDbConfigWrapper';

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
      ConfigComponent: IMDbConfigWrapper,
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
    // Remove credentials using unified API
    try {
      await fetch(`${API_BASE}/api/credentials/imdb`, {
        method: 'DELETE',
      });
    } catch (error) {
      console.error('Failed to disconnect IMDb:', error);
    }
  }

  private async checkConnection(): Promise<boolean> {
    // Check localStorage first (for backward compatibility)
    const localConfig = localStorage.getItem('imdb_configured');
    if (localConfig) return true;

    // Check using unified credentials API
    try {
      const response = await fetch(`${API_BASE}/api/credentials/imdb/api-key`);
      if (response.ok) {
        const data = await response.json();
        if (data.has_key) {
          // Update localStorage for consistency
          localStorage.setItem('imdb_configured', 'true');
          return true;
        }
      }
    } catch (error) {
      console.error('Failed to check IMDb connection:', error);
    }
    return false;
  }
}

export const imdbIntegration = new IMDbIntegrationProvider();
