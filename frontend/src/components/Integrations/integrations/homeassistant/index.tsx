import { SiHomeassistant } from 'react-icons/si';
import { Integration, IntegrationProvider, IntegrationConfig, IntegrationScope } from '../types';
import { HomeAssistantIntegration as HomeAssistantIntegrationComponent } from '@/components/HomeAssistantIntegration/HomeAssistantIntegration';
import { API_BASE_URL, withAgentExecutionScope } from '@/lib/api';

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

  getConfig(scope?: IntegrationScope): IntegrationConfig {
    const agentName = scope?.agentName ?? null;
    const executionScope = scope?.executionScope;
    return {
      integration: this.integration,
      onDisconnect: async () => {
        const response = await fetch(
          withAgentExecutionScope(
            `${API_BASE_URL}/api/homeassistant/disconnect`,
            agentName,
            executionScope
          ),
          {
            method: 'POST',
          }
        );
        if (!response.ok) {
          throw new Error('Failed to disconnect Home Assistant');
        }
      },
      ConfigComponent: props => (
        <HomeAssistantIntegrationComponent
          onSuccess={props.onSuccess}
          agentName={agentName}
          executionScope={executionScope}
        />
      ),
    };
  }

  async loadStatus(scope?: IntegrationScope): Promise<Partial<Integration>> {
    const agentName = scope?.agentName ?? null;
    const executionScope = scope?.executionScope;
    try {
      const response = await fetch(
        withAgentExecutionScope(
          `${API_BASE_URL}/api/homeassistant/status`,
          agentName,
          executionScope
        )
      );
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
}

export const homeAssistantIntegration = new HomeAssistantIntegrationProvider();
