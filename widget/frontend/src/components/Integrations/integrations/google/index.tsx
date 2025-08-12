import { FaGoogle } from 'react-icons/fa';
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { API_BASE } from '@/lib/api';
import { GoogleIntegration as GoogleIntegrationComponent } from '@/components/GoogleIntegration/GoogleIntegration';

// Wrapper component to handle the dialog integration
function GoogleConfigDialog(_props: { onClose: () => void; onSuccess?: () => void }) {
  // GoogleIntegrationComponent handles its own closing logic
  return <GoogleIntegrationComponent />;
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
      ConfigComponent: GoogleConfigDialog,
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    try {
      const response = await fetch(`${API_BASE}/api/gmail/status`);
      if (response.ok) {
        const data = await response.json();
        if (data.configured) {
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
      const response = await fetch(`${API_BASE}/api/gmail/status`);
      if (response.ok) {
        const data = await response.json();
        return data.configured === true;
      }
    } catch (error) {
      console.error('Failed to check Google connection:', error);
    }
    return false;
  }
}

export const googleIntegration = new GoogleIntegrationProvider();
