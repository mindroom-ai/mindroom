import { FaGoogle } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig, IntegrationScope } from '../types';
import { GoogleIntegration as GoogleIntegrationComponent } from '@/components/GoogleIntegration/GoogleIntegration';
import { withAgentName } from '@/lib/api';

// Wrapper component to handle the dialog integration
function GoogleConfigDialog(props: {
  onClose: () => void;
  onSuccess?: () => void;
  agentName?: string | null;
}) {
  // Pass the onSuccess callback to the GoogleIntegrationComponent
  return <GoogleIntegrationComponent onSuccess={props.onSuccess} agentName={props.agentName} />;
}

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
      onAction: async () => {
        // The parent component will handle showing the dialog
        // This is handled via the ConfigComponent
      },
      onDisconnect: async () => {
        const response = await fetch(withAgentName('/api/google/disconnect', agentName), {
          method: 'POST',
        });
        if (!response.ok) {
          throw new Error('Failed to disconnect Google services');
        }
      },
      ConfigComponent: props => <GoogleConfigDialog {...props} agentName={agentName} />,
      checkConnection: () => this.checkConnection(agentName),
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

  private async checkConnection(agentName?: string | null): Promise<boolean> {
    try {
      const response = await fetch(withAgentName('/api/google/status', agentName));
      if (response.ok) {
        const data = await response.json();
        return data.connected === true;
      }
    } catch (error) {
      console.error('Failed to check Google connection:', error);
    }
    return false;
  }
}

export const googleIntegration = new GoogleIntegrationProvider();
