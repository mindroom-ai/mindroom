import { SiHomeassistant } from 'react-icons/si';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { HomeAssistantIntegration as HomeAssistantIntegrationComponent } from '@/components/HomeAssistantIntegration/HomeAssistantIntegration';

// Wrapper component to handle the dialog integration
function HomeAssistantConfigDialog(props: { onClose: () => void; onSuccess?: () => void }) {
  // Pass the onSuccess callback to the HomeAssistantIntegrationComponent
  return <HomeAssistantIntegrationComponent onSuccess={props.onSuccess} />;
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
      onDisconnect: async () => {
        const response = await fetch('/api/homeassistant/disconnect', {
          method: 'POST',
        });
        if (!response.ok) {
          throw new Error('Failed to disconnect Home Assistant');
        }
      },
      ConfigComponent: HomeAssistantConfigDialog,
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    try {
      const response = await fetch('/api/homeassistant/status');
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
      const response = await fetch('/api/homeassistant/status');
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
