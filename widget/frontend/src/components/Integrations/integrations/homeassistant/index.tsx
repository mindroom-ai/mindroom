import { SiHomeassistant } from 'react-icons/si';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';
import { HomeAssistantIntegration as HomeAssistantIntegrationComponent } from '@/components/HomeAssistantIntegration/HomeAssistantIntegration';

// Wrapper component to handle the dialog integration
function HomeAssistantConfigDialog(_props: { onClose: () => void; onSuccess?: () => void }) {
  // HomeAssistantIntegrationComponent handles its own closing logic
  return <HomeAssistantIntegrationComponent />;
}

class HomeAssistantIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'homeassistant',
    name: 'Home Assistant',
    description: 'Control and monitor your smart home devices',
    category: 'smart_home',
    icon: <SiHomeassistant className="h-5 w-5" />,
    status: 'available',
    setup_type: 'special',
    connected: false,
  };

  getConfig(): IntegrationConfig {
    return {
      integration: this.integration,
      onAction: async () => {
        // The parent component will handle showing the dialog
        // This is handled via the ConfigComponent
      },
      ConfigComponent: HomeAssistantConfigDialog,
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/status`);
      if (response.ok) {
        const data = await response.json();
        if (data.connected) {
          return {
            status: 'connected',
            connected: true,
            details: {
              instance_url: data.instance_url,
              version: data.version,
              location_name: data.location_name,
              entities_count: data.entities_count,
            },
          };
        }
      }
    } catch (error) {
      console.error('Failed to load Home Assistant status:', error);
    }
    return {
      status: 'available',
      connected: false,
    };
  }

  private async checkConnection(): Promise<boolean> {
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/status`);
      if (response.ok) {
        const data = await response.json();
        return data.connected === true;
      }
    } catch (error) {
      console.error('Failed to check Home Assistant connection:', error);
    }
    return false;
  }
}

export const homeAssistantIntegration = new HomeAssistantIntegrationProvider();
