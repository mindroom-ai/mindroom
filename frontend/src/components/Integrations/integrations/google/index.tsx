import { FaGoogle } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig, IntegrationScope } from '../types';
import { GoogleIntegration as GoogleIntegrationComponent } from '@/components/GoogleIntegration/GoogleIntegration';
import { withAgentName } from '@/lib/api';

class GoogleIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'google',
    name: 'Google Services',
    description: 'Gmail, Calendar, and Drive integration',
    category: 'email',
    icon: <FaGoogle className="h-5 w-5" />,
    status: 'available',
    setup_type: 'special',
    connected: false,
  };

  getConfig(scope?: IntegrationScope): IntegrationConfig {
    const agentName = scope?.agentName ?? null;
    return {
      integration: this.integration,
      onDisconnect: async () => {
        const response = await fetch(withAgentName('/api/google/disconnect', agentName), {
          method: 'POST',
        });
        if (!response.ok) {
          throw new Error('Failed to disconnect Google services');
        }
      },
      ConfigComponent: props => (
        <GoogleIntegrationComponent onSuccess={props.onSuccess} agentName={agentName} />
      ),
    };
  }

  async loadStatus(scope?: IntegrationScope): Promise<Partial<Integration>> {
    const agentName = scope?.agentName ?? null;
    try {
      const response = await fetch(withAgentName('/api/google/status', agentName));
      if (response.ok) {
        const data = await response.json();
        if (data.connected) {
          return {
            status: 'connected',
            connected: true,
          };
        }
      }
    } catch (error) {
      console.error('Failed to load Google status:', error);
    }
    return {
      status: 'available',
      connected: false,
    };
  }
}

export const googleIntegration = new GoogleIntegrationProvider();
