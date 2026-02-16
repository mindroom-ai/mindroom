import { FaGoogle } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { GoogleIntegration as GoogleIntegrationComponent } from '@/components/GoogleIntegration/GoogleIntegration';

// Wrapper component to handle the dialog integration
function GoogleConfigDialog(props: { onClose: () => void; onSuccess?: () => void }) {
  // Pass the onSuccess callback to the GoogleIntegrationComponent
  return <GoogleIntegrationComponent onSuccess={props.onSuccess} />;
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

  getConfig(): IntegrationConfig {
    return {
      integration: this.integration,
      onAction: async () => {
        // The parent component will handle showing the dialog
        // This is handled via the ConfigComponent
      },
      onDisconnect: async () => {
        const response = await fetch('/api/google/disconnect', {
          method: 'POST',
        });
        if (!response.ok) {
          throw new Error('Failed to disconnect Google services');
        }
      },
      ConfigComponent: GoogleConfigDialog,
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    try {
      const response = await fetch('/api/google/status');
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

  private async checkConnection(): Promise<boolean> {
    try {
      const response = await fetch('/api/google/status');
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
